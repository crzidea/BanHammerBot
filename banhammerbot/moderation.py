import logging
import time
from collections import OrderedDict
from typing import Any

from .api import APIError, ImageAsset, Telegram
from .config import Config
from .decision import InvalidDecision, Jev
from .storage import Store

log = logging.getLogger(__name__)
ADMINS = {"creator", "administrator"}
MEMBERS = {"member", "restricted", "left"}
MEDIA_TYPES = ("photo", "document", "video", "animation", "sticker", "audio", "voice", "video_note")


def message_summary(message: dict) -> dict:
    """Only this sender's content is included; replies/forwarded sender identities are excluded."""
    links = []
    for text_key, entity_key in (("text", "entities"), ("caption", "caption_entities")):
        raw = message.get(text_key, "").encode("utf-16-le")
        for entity in message.get(entity_key, []):
            if entity.get("type") == "text_link":
                links.append(entity.get("url", "")[:2048])
            elif entity.get("type") in {"url", "mention", "email"}:
                start = entity["offset"] * 2
                end = start + entity["length"] * 2
                links.append(raw[start:end].decode("utf-16-le", errors="replace")[:2048])
    result = {
        "message_id": message["message_id"],
        "sent_at": message.get("date"),
        "edited_at": message.get("edit_date"),
        "text": message.get("text", "")[:8192],
        "caption": message.get("caption", "")[:2048],
        "links": links[:30],
        "media_types": [key for key in MEDIA_TYPES if key in message],
        "forwarded": "forward_origin" in message,
    }
    if "document" in message:
        result["document_name"] = message["document"].get("file_name", "")[:256]
    if "sticker" in message:
        result["sticker_emoji"] = message["sticker"].get("emoji", "")
    if "poll" in message:
        poll = message["poll"]
        result["poll"] = {
            "question": poll.get("question", "")[:1024],
            "options": [o.get("text", "")[:256] for o in poll.get("options", [])[:12]],
        }
    if "contact" in message:
        result["contact"] = {
            key: message["contact"].get(key, "")[:256]
            for key in ("first_name", "last_name", "phone_number")
        }
    return result


def choose_photo(photos: list[dict]) -> str | None:
    if not photos:
        return None
    # Prefer enough pixels to read promotional text without uploading the full-size photo.
    adequate = [p for p in photos if max(p["width"], p["height"]) >= 640]
    selected = min(adequate, key=lambda p: p["width"] * p["height"]) if adequate else photos[-1]
    return selected["file_id"]


class Moderator:
    def __init__(self, config: Config, telegram: Telegram, jev: Jev, store: Store):
        self.config = config
        self.telegram = telegram
        self.jev = jev
        self.store = store
        self.username = ""
        self.history: OrderedDict[tuple[int, int], list[tuple[float, dict]]] = OrderedDict()
        self.avatars: OrderedDict[int, tuple[float, ImageAsset | None]] = OrderedDict()
        self.jev_unavailable_until = 0.0

    def recent(self, key: tuple[int, int], summary: dict, now: float) -> list[dict]:
        for existing_key, entries in list(self.history.items()):
            valid = [(t, m) for t, m in entries if now - t < self.config.history_ttl]
            if valid:
                self.history[existing_key] = valid
            else:
                del self.history[existing_key]
        entries = self.history.pop(key, [])
        # An edit replaces the original, rather than looking like another repeated message.
        entries = [(t, m) for t, m in entries if m["message_id"] != summary["message_id"]]
        previous = [m for _, m in entries]
        self.history[key] = (entries + [(now, summary)])[-self.config.history_limit :]
        while len(self.history) > 256:
            self.history.popitem(last=False)
        return previous

    async def avatar(self, user_id: int) -> ImageAsset | None:
        now = time.monotonic()
        for key, (deadline, _) in list(self.avatars.items()):
            if deadline <= now:
                del self.avatars[key]
        if user_id in self.avatars:
            self.avatars.move_to_end(user_id)
            return self.avatars[user_id][1]
        photos = await self.telegram.call("getUserProfilePhotos", user_id=user_id, limit=1)
        file_id = choose_photo(photos["photos"][0]) if photos.get("photos") else None
        asset = await self.telegram.image(file_id) if file_id else None
        self.avatars[user_id] = (now + self.config.avatar_cache_seconds, asset)
        while len(self.avatars) > 32:
            self.avatars.popitem(last=False)
        return asset

    async def images(self, message: dict) -> tuple[dict[str, ImageAsset], dict[str, str]]:
        images = {}
        status = {}
        try:
            avatar = await self.avatar(message["from"]["id"])
            if avatar:
                images["sender_avatar"] = avatar
            status["avatar"] = "available" if avatar else "not_visible"
        except APIError:
            status["avatar"] = "unavailable"
        file_id = choose_photo(message.get("photo", []))
        document = message.get("document", {})
        if document.get("mime_type") in {"image/jpeg", "image/png"}:
            file_id = document.get("file_id")
        if file_id:
            try:
                images["current_message_image"] = await self.telegram.image(file_id)
                status["message_image"] = "available"
            except APIError:
                status["message_image"] = "unavailable"
        elif any(key in message for key in MEDIA_TYPES):
            status["message_image"] = "unsupported_media"
        return images, status

    async def command(self, message: dict) -> bool:
        parts = message.get("text", "").split()
        if not parts:
            return False
        command, _, target = parts[0].partition("@")
        if target and target.lower() != self.username.lower():
            return False
        if command not in {"/status", "/unban", "/help"}:
            return False
        chat_id = message["chat"]["id"]
        if command == "/unban":
            if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
                text = "Usage: /unban <numeric user ID>"
            elif int(parts[1]) <= 0:
                text = "The user ID must be positive."
            elif self.config.dry_run:
                text = "Dry run: unban was not executed. Set DRY_RUN=false to enable changes."
            else:
                await self.telegram.call(
                    "unbanChatMember",
                    chat_id=chat_id,
                    user_id=int(parts[1]),
                    only_if_banned=True,
                )
                text = f"Unbanned user {parts[1]}. They may rejoin using an invite."
        else:
            text = (
                f"BanHammerBot: {'dry run' if self.config.dry_run else 'live'}\n"
                f"Jev: {self.config.model}\nBan threshold: {self.config.ban_threshold:.2f}\n"
                "Analyzes names, avatars, messages, and photos.\n"
                "Admin commands: /status, /help, /unban <user ID>"
            )
        await self.telegram.call("sendMessage", chat_id=chat_id, text=text)
        return True

    def record(
        self,
        update_id: int,
        message: dict,
        action: str,
        decision: Any = None,
        detail: str | None = None,
    ) -> None:
        self.store.record(
            update_id,
            message,
            action,
            decision.as_dict() if decision else None,
            detail,
        )
        log.info(
            "update=%s chat=%s user=%s message=%s action=%s violation=%s detail=%s",
            update_id,
            message["chat"]["id"],
            message["from"]["id"],
            message["message_id"],
            action,
            round(decision.violation, 4) if decision else None,
            detail,
        )

    async def handle(self, update: dict) -> None:
        update_id = update["update_id"]
        if self.store.completed(update_id):
            return
        message = update.get("message") or update.get("edited_message")
        if not message or message.get("chat", {}).get("type") not in {"group", "supergroup"}:
            return
        chat_id = message["chat"]["id"]
        user = message.get("from")
        if chat_id not in self.config.chat_ids or not user or message.get("sender_chat"):
            return
        if user.get("is_bot"):
            return
        # Service messages are not evidence that a user sent spam.
        if not any(key in message for key in (*MEDIA_TYPES, "text", "caption", "poll", "contact")):
            return
        now = time.time()
        event_time = message.get("edit_date", message.get("date", 0))
        if now - event_time > self.config.max_event_age:
            self.record(update_id, message, "stale")
            return
        user_id = user["id"]
        try:
            member = await self.telegram.call("getChatMember", chat_id=chat_id, user_id=user_id)
            if member["status"] in ADMINS:
                await self.command(message)
                return
            if user_id in self.config.exempt_user_ids or member["status"] not in MEMBERS:
                return
            summary = message_summary(message)
            previous = self.recent((chat_id, user_id), summary, now)
            if time.monotonic() < self.jev_unavailable_until:
                self.record(update_id, message, "error", detail="jev_rate_limit_cooldown")
                return
            images, image_status = await self.images(message)
            evidence = {
                "sender": {
                    key: user.get(key, "") for key in ("id", "first_name", "last_name", "username")
                },
                "chat": {"id": chat_id, "title": message["chat"].get("title", "")},
                "current_message": summary,
                "recent_messages_by_same_sender": previous,
                "image_status": image_status,
            }
            decision = await self.jev.decide(evidence, images)
            if decision.violation < self.config.ban_threshold:
                action = "review" if decision.violation >= self.config.review_threshold else "allow"
                self.record(update_id, message, action, decision)
                return
            if self.config.dry_run:
                self.record(update_id, message, "would_ban", decision)
                return
            # A user may have become an administrator while the model was evaluating them.
            member = await self.telegram.call("getChatMember", chat_id=chat_id, user_id=user_id)
            if member["status"] not in MEMBERS:
                self.record(update_id, message, "exempt_after_decision", decision)
                return
            self.record(update_id, message, "ban_pending", decision)
            await self.telegram.call(
                "banChatMember",
                chat_id=chat_id,
                user_id=user_id,
                revoke_messages=True,
            )
            # Supergroups revoke message history on ban. Explicit deletion also covers basic groups.
            try:
                await self.telegram.call(
                    "deleteMessage",
                    chat_id=chat_id,
                    message_id=message["message_id"],
                )
            except APIError as exc:
                # A successful ban must remain recorded even when deletion fails or is redundant.
                self.record(update_id, message, "banned", decision, f"delete_code={exc.code}")
            else:
                self.record(update_id, message, "banned", decision)
        except APIError as exc:
            if exc.retry_after and exc.code == 429 and str(exc).startswith("jev"):
                self.jev_unavailable_until = time.monotonic() + exc.retry_after
            self.record(update_id, message, "error", detail=str(exc))
        except InvalidDecision:
            self.record(update_id, message, "error", detail="invalid_jev_decision")
