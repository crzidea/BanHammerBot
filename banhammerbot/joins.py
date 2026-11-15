import logging
import time

from .api import APIError, Telegram
from .config import Config
from .permissions import has_moderation_rights
from .storage import Store

log = logging.getLogger(__name__)
MUTE_PERMISSIONS = dict.fromkeys(
    (
        "can_send_messages",
        "can_send_audios",
        "can_send_documents",
        "can_send_photos",
        "can_send_videos",
        "can_send_video_notes",
        "can_send_voice_notes",
        "can_send_polls",
        "can_send_other_messages",
        "can_add_web_page_previews",
    ),
    False,
)


def is_present(member: dict) -> bool:
    return member.get("status") in {"member", "administrator", "creator"} or (
        member.get("status") == "restricted" and member.get("is_member") is True
    )


class JoinMute:
    def __init__(self, config: Config, telegram: Telegram, store: Store, bot_id: int):
        self.config, self.telegram, self.store, self.bot_id = config, telegram, store, bot_id

    async def mute(self, chat: dict, user: dict, joined_at: int) -> None:
        chat_id, user_id = chat["id"], user["id"]
        if (
            chat.get("type") != "supergroup"
            or user.get("is_bot")
            or user_id in self.config.exempt_user_ids
            or user_id == self.bot_id
            or (self.config.chat_ids and chat_id not in self.config.chat_ids)
        ):
            return
        settings = self.store.settings(chat_id, self.config.dry_run)
        if not settings.join_mute_seconds or self.store.join_handled(chat_id, user_id, joined_at):
            return
        now = int(time.time())
        until_date = joined_at + settings.join_mute_seconds
        if now - joined_at > self.config.max_event_age or until_date - now < 45:
            return
        try:
            bot = await self.telegram.call("getChatMember", chat_id=chat_id, user_id=self.bot_id)
            if not has_moderation_rights(bot):
                return
            member = await self.telegram.call("getChatMember", chat_id=chat_id, user_id=user_id)
            # Do not shorten/overwrite restrictions applied by another admin or moderation bot.
            if member.get("status") != "member" or member.get("user", {}).get("is_bot"):
                return
            if settings.dry_run:
                action = "would_mute"
            else:
                # Telegram treats near-expired deadlines as permanent. Keep a margin and use
                # a single bounded HTTP attempt for restrictions (see Telegram.call).
                if until_date - int(time.time()) < 45:
                    return
                await self.telegram.call(
                    "restrictChatMember",
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=MUTE_PERMISSIONS,
                    use_independent_chat_permissions=True,
                    until_date=until_date,
                )
                action = "muted"
        except APIError as exc:
            action = "error"
            log.warning("Join mute failed chat=%s user=%s: %s", chat_id, user_id, exc)
        self.store.record_join(chat_id, user_id, joined_at, action, until_date)
        log.info("join chat=%s user=%s action=%s until=%s", chat_id, user_id, action, until_date)

    async def handle(self, update: dict) -> bool:
        event = update.get("my_chat_member") or update.get("chat_member")
        if event:
            self.store.remember_group(event["chat"])
            if "my_chat_member" in update:
                return True
            new = event["new_chat_member"]
            if not is_present(event["old_chat_member"]) and is_present(new):
                await self.mute(event["chat"], new["user"], event["date"])
            return True
        message = update.get("message", {})
        if "new_chat_members" in message:
            self.store.remember_group(message["chat"])
            for user in message["new_chat_members"]:
                await self.mute(message["chat"], user, message["date"])
            return True
        return False
