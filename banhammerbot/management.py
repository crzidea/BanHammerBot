import logging
import re
from html import escape

from .api import APIError, Telegram
from .config import Config
from .permissions import can_manage, has_moderation_rights
from .storage import Store

log = logging.getLogger(__name__)
JOIN_MUTE_PRESETS = (4 * 3600, 12 * 3600, 24 * 3600)
BAN_LOG_PAGE_SIZE = 5
PRIVATE_COMMANDS = [
    ("start", "Open the management menu"),
    ("menu", "Manage your selected group"),
    ("groups", "List groups you can manage"),
    ("select", "Select a group: /select or /select -100…"),
    ("status", "Show the selected group's settings"),
    ("banlog", "Show recent bans and dry-run ban decisions"),
    ("dryrun", "Set selected group dry run: on or off"),
    ("joinmute", "Mute new members: on, off, 4h, 12h, or 24h"),
    ("unban", "Unban a user in the selected group"),
    ("help", "Show management commands"),
]
HELP = (
    "Manage a group where you are the owner or an admin with Ban users permission.\n\n"
    "/select — choose a group\n/select -100… — select by numeric group ID\n"
    "/menu or /status — current settings\n"
    "/banlog — recent bans and dry-run ban decisions\n"
    "/dryrun on — observe without enforcement\n"
    "/dryrun off — enable enforcement for this group\n"
    "/joinmute 4h — mute new members for 4 hours\n"
    "/joinmute 12h or /joinmute 24h — longer mute durations\n"
    "/joinmute on — enable the saved mute duration\n"
    "/joinmute off — disable the new-member mute\n"
    "/unban <user ID> — unban in the selected group\n\n"
    "Durations: 60s to 30d (s, m, h, d). Join mute requires a supergroup. "
    "Dry run also skips mutes."
)


class ManagementError(ValueError):
    """A user-facing validation or authorization failure."""


def duration(value: str) -> int:
    if value.lower() == "off":
        return 0
    match = re.fullmatch(r"([1-9][0-9]{0,7})([smhd]?)", value.lower())
    if match:
        seconds = int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
        if 60 <= seconds <= 30 * 86400:
            return seconds
    raise ManagementError("Use /joinmute off or a duration from 60s to 30d, such as /joinmute 4h.")


def format_duration(seconds: int) -> str:
    if not seconds:
        return "Off"
    if seconds in JOIN_MUTE_PRESETS:
        return f"{seconds // 3600} hours"
    for name, unit in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds % unit == 0:
            count = seconds // unit
            return f"{count} {name}{'s' if count != 1 else ''}"
    raise ValueError("Duration must be an integer number of seconds")


class Management:
    def __init__(self, config: Config, telegram: Telegram, store: Store, bot_id: int):
        self.config, self.telegram, self.store, self.bot_id = config, telegram, store, bot_id
        self.username = ""

    async def register(self) -> None:
        await self.telegram.call(
            "setMyCommands",
            scope={"type": "all_private_chats"},
            commands=[
                {"command": command, "description": description}
                for command, description in PRIVATE_COMMANDS
            ],
        )
        await self.telegram.call(
            "setMyCommands",
            scope={"type": "all_chat_administrators"},
            commands=[
                {"command": command, "description": description}
                for command, description in PRIVATE_COMMANDS
                if command in {"menu", "status", "banlog", "unban", "help"}
            ],
        )
        await self.telegram.call("setChatMenuButton", menu_button={"type": "commands"})

    async def authorize(self, user_id: int, chat_id: int) -> dict:
        if chat_id >= 0 or (self.config.chat_ids and chat_id not in self.config.chat_ids):
            raise ManagementError("This group is outside the bot's configured scope.")
        # Verify the human before fetching or revealing group titles or settings.
        member = await self.telegram.call("getChatMember", chat_id=chat_id, user_id=user_id)
        if not can_manage(member):
            raise ManagementError(
                "You must be the group owner or an admin with Ban users permission."
            )
        bot = await self.telegram.call("getChatMember", chat_id=chat_id, user_id=self.bot_id)
        if not has_moderation_rights(bot):
            raise ManagementError(
                "Give the bot Ban users and Delete messages admin permissions first."
            )
        chat = await self.telegram.call("getChat", chat_id=chat_id)
        if chat.get("type") not in {"group", "supergroup"}:
            raise ManagementError("Choose a group or supergroup.")
        self.store.remember_group(chat)
        return chat

    async def send(
        self,
        user_id: int,
        text: str,
        rows: list | None = None,
        message_id: int | None = None,
        *,
        parse_mode: str | None = None,
    ) -> None:
        params = {"chat_id": user_id, "text": text}
        if parse_mode is not None:
            params["parse_mode"] = parse_mode
            params["link_preview_options"] = {"is_disabled": True}
        if rows is not None:
            params["reply_markup"] = {"inline_keyboard": rows}
        if message_id is not None:
            try:
                await self.telegram.call("editMessageText", message_id=message_id, **params)
                return
            except APIError as exc:
                description = (exc.description or "").lower()
                if exc.code == 400 and "message is not modified" in description:
                    return
                if exc.code != 400 or not any(
                    reason in description
                    for reason in (
                        "message to edit not found",
                        "message can't be edited",
                    )
                ):
                    raise
                # A deleted or uneditable menu is replaced with a fresh message.
        await self.telegram.call("sendMessage", **params)

    async def groups(self, user_id: int, offset: int = 0, message_id: int | None = None) -> None:
        ids = self.store.groups(offset, 11)
        selected = self.store.selected_group(user_id)
        rows = []
        for chat_id in ids[:10]:
            try:
                chat = await self.authorize(user_id, chat_id)
            except (ManagementError, APIError):
                continue
            rows.append(
                [
                    {
                        "text": ("✓ " if chat_id == selected else "")
                        + chat.get("title", str(chat_id))[:62],
                        "callback_data": f"select:{chat_id}",
                    }
                ]
            )
        navigation = []
        if offset:
            navigation.append(
                {"text": "Previous", "callback_data": f"groups:{max(0, offset - 10)}"}
            )
        if len(ids) > 10:
            navigation.append({"text": "Next", "callback_data": f"groups:{offset + 10}"})
        if navigation:
            rows.append(navigation)
        await self.send(
            user_id,
            "Select a group\n\n"
            "Choose the group whose settings you want to manage.\n\n"
            "If it is missing, send /status in that group or use /select <numeric group ID>.",
            rows,
            message_id,
        )

    async def panel(
        self,
        user_id: int,
        chat: dict,
        notice: str = "",
        message_id: int | None = None,
    ) -> None:
        chat_id = chat["id"]
        settings = self.store.settings(chat_id, self.config.dry_run)
        text = (
            f"{chat.get('title', chat_id)}\n"
            "Group settings\n\n"
            f"Dry run: {'On · preview only' if settings.dry_run else 'Off · enforcement enabled'}\n"
            f"Mute new members: {format_duration(settings.join_mute_seconds)}"
        )
        if settings.dry_run and settings.join_mute_seconds:
            text += "\n\nTurn dry run off to apply bans and mutes."
        if notice:
            text += "\n\n" + notice
        rows = [
            [
                {"text": "Dry run", "callback_data": f"label:dry:{chat_id}"},
                {
                    "text": ("✓ " if settings.dry_run else "") + "On",
                    "callback_data": f"dry:{chat_id}:1",
                },
                {
                    "text": ("✓ " if not settings.dry_run else "") + "Off",
                    "callback_data": f"dry:{chat_id}:0",
                },
            ],
        ]
        if chat["type"] == "supergroup":
            rows.extend(
                [
                    [
                        {"text": "Join mute", "callback_data": f"label:mute:{chat_id}"},
                        {
                            "text": ("✓ " if not settings.join_mute_seconds else "") + "Off",
                            "callback_data": f"mute:{chat_id}:0",
                        },
                    ],
                    [
                        {
                            "text": ("✓ " if settings.join_mute_seconds == seconds else "")
                            + format_duration(seconds),
                            "callback_data": f"join:{chat_id}:{seconds}",
                        }
                        for seconds in JOIN_MUTE_PRESETS
                    ],
                ]
            )
        rows.append([{"text": "Recent ban log", "callback_data": f"banlog:{chat_id}:0"}])
        rows.append(
            [
                {"text": "Change group", "callback_data": "groups:0"},
                {"text": "Refresh", "callback_data": f"menu:{chat_id}"},
            ]
        )
        await self.send(user_id, text, rows, message_id)

    async def ban_log(
        self,
        user_id: int,
        chat: dict,
        offset: int = 0,
        message_id: int | None = None,
    ) -> None:
        chat_id = chat["id"]
        entries = self.store.recent_bans(chat_id, offset, BAN_LOG_PAGE_SIZE + 1)
        sections = [
            f"<b>{escape(str(chat.get('title', chat_id)))}</b>\n"
            "Recent ban log · times in UTC\n"
            "Bans and dry-run decisions recorded by this bot."
        ]
        for entry in entries[:BAN_LOG_PAGE_SIZE]:
            outcome = "Banned" if entry.action == "banned" else "Dry run: would ban (no action)"
            text = (
                f"<b>{outcome}</b> · {escape(entry.created_at)} UTC\n"
                f'User: <a href="tg://user?id={entry.user_id}">'
                f"{escape(entry.user_name or str(entry.user_id))}</a>\n"
            )
            message = str(entry.message_id)
            if chat["type"] == "supergroup" and str(chat_id).startswith("-100"):
                channel_id = str(chat_id)[4:]
                message = f'<a href="https://t.me/c/{channel_id}/{entry.message_id}">{message}</a>'
            text += f"Message: {message}"
            scores = [(entry.decision or {}).get(key) for key in ("unsolicited_ad", "spam")]
            if all(type(score) in (float, int) and 0 <= score <= 1 for score in scores):
                text += f" · Violation: {max(scores):.2f}"
            sections.append(text)
        if not entries:
            sections.append(
                "No older entries."
                if offset
                else "No bans or dry-run ban decisions recorded for this group yet."
            )
        else:
            sections.append(
                "Message links may be unavailable after deletion. "
                "User links depend on Telegram privacy settings."
            )
        rows = []
        navigation = []
        if offset:
            navigation.append(
                {
                    "text": "Newer",
                    "callback_data": f"banlog:{chat_id}:{max(0, offset - BAN_LOG_PAGE_SIZE)}",
                }
            )
        if len(entries) > BAN_LOG_PAGE_SIZE:
            navigation.append(
                {"text": "Older", "callback_data": f"banlog:{chat_id}:{offset + BAN_LOG_PAGE_SIZE}"}
            )
        if navigation:
            rows.append(navigation)
        rows.append(
            [
                {"text": "Back to menu", "callback_data": f"menu:{chat_id}"},
                {"text": "Refresh", "callback_data": f"banlog:{chat_id}:0"},
            ]
        )
        await self.send(user_id, "\n\n".join(sections), rows, message_id, parse_mode="HTML")

    async def selected(self, user_id: int) -> dict:
        chat_id = self.store.selected_group(user_id)
        if chat_id is None:
            raise ManagementError("Select a group first with /select.")
        return await self.authorize(user_id, chat_id)

    def set_join_mute(self, chat: dict, seconds: int) -> None:
        if seconds and chat["type"] != "supergroup":
            raise ManagementError("Timed join mute requires a Telegram supergroup.")
        self.store.set_join_mute(chat["id"], seconds)

    async def command(self, user_id: int, text: str) -> None:
        parts = text.split()
        if not parts:
            return
        name, _, target = parts[0].partition("@")
        if target and target.lower() != self.username.lower():
            return
        args = parts[1:]
        if name == "/start" and args and re.fullmatch(r"group_-[0-9]{1,16}", args[0]):
            name, args = "/select", [args[0].removeprefix("group_")]
        if name == "/start" and len(args) == 1 and re.fullmatch(r"banlog_-[0-9]{1,16}", args[0]):
            chat = await self.authorize(user_id, int(args[0].removeprefix("banlog_")))
            self.store.select_group(user_id, chat["id"])
            await self.ban_log(user_id, chat)
            return
        if name in {"/groups", "/select"} and not args:
            await self.groups(user_id)
        elif name == "/select":
            if len(args) != 1 or not re.fullmatch(r"-[0-9]{1,16}", args[0]):
                raise ManagementError(
                    "Use /select to choose a group, or /select <negative group ID>."
                )
            chat = await self.authorize(user_id, int(args[0]))
            self.store.select_group(user_id, chat["id"])
            await self.panel(user_id, chat)
        elif name in {"/start", "/menu", "/status"}:
            if self.store.selected_group(user_id) is None:
                await self.groups(user_id)
            else:
                await self.panel(user_id, await self.selected(user_id))
        elif name == "/banlog":
            if args:
                raise ManagementError("Use /banlog, then Older or Newer to browse entries.")
            await self.ban_log(user_id, await self.selected(user_id))
        elif name == "/dryrun":
            chat = await self.selected(user_id)
            if args:
                if len(args) != 1 or args[0].lower() not in {"on", "off"}:
                    raise ManagementError("Use /dryrun on or /dryrun off.")
                self.store.set_dry_run(chat["id"], args[0].lower() == "on")
            await self.panel(user_id, chat)
        elif name == "/joinmute":
            chat = await self.selected(user_id)
            if args:
                if len(args) != 1:
                    raise ManagementError("Use /joinmute on, off, 4h, 12h, or 24h.")
                seconds = (
                    self.store.settings(chat["id"], self.config.dry_run).join_mute_duration
                    if args[0].lower() == "on"
                    else duration(args[0])
                )
                self.set_join_mute(chat, seconds)
            await self.panel(user_id, chat)
        elif name == "/unban":
            chat = await self.selected(user_id)
            if len(args) != 1 or not re.fullmatch(r"[1-9][0-9]{0,15}", args[0]):
                raise ManagementError("Use /unban <numeric user ID>.")
            if self.store.settings(chat["id"], self.config.dry_run).dry_run:
                await self.panel(user_id, chat, "Dry run: user was not unbanned.")
            else:
                await self.telegram.call(
                    "unbanChatMember", chat_id=chat["id"], user_id=int(args[0]), only_if_banned=True
                )
                await self.panel(user_id, chat, f"Unbanned user {args[0]}.")
        else:
            await self.send(
                user_id, HELP, [[{"text": "Choose group", "callback_data": "groups:0"}]]
            )

    async def callback(self, user_id: int, data: str, message_id: int | None = None) -> None:
        # Labels have distinct callback data so they do not share Refresh's loading state.
        # handle() has already acknowledged the tap; labels do not perform any action.
        if re.fullmatch(r"label:(dry|mute):-[0-9]{1,16}", data):
            return
        if re.fullmatch(r"groups:[0-9]{1,6}", data):
            await self.groups(user_id, int(data.split(":")[1]), message_id)
            return
        match = re.fullmatch(r"banlog:(-[0-9]{1,16}):([0-9]{1,8})", data)
        if match:
            chat_id, offset = map(int, match.groups())
            chat = await self.authorize(user_id, chat_id)
            self.store.select_group(user_id, chat_id)
            await self.ban_log(user_id, chat, offset, message_id)
            return
        match = re.fullmatch(r"(select|menu|dry|join|mute):(-[0-9]{1,16})(?::([0-9]{1,8}))?", data)
        if not match:
            raise ManagementError("This menu action is invalid. Open /menu again.")
        action, chat_id, value = match.groups()
        chat = await self.authorize(user_id, int(chat_id))
        if action == "dry":
            if value not in {"0", "1"}:
                raise ManagementError("Invalid dry-run setting.")
            self.store.set_dry_run(chat["id"], value == "1")
        elif action == "join":
            seconds = 0 if value == "0" else duration(value or "")
            self.set_join_mute(chat, seconds)
        elif action == "mute":
            if value not in {"0", "1"}:
                raise ManagementError("Invalid join-mute setting.")
            seconds = (
                self.store.settings(chat["id"], self.config.dry_run).join_mute_duration
                if value == "1"
                else 0
            )
            self.set_join_mute(chat, seconds)
        elif value is not None:
            raise ManagementError("This menu action is invalid. Open /menu again.")
        # Carry the target in each button so old menus cannot change a different group.
        self.store.select_group(user_id, chat["id"])
        await self.panel(user_id, chat, message_id=message_id)

    async def handle(self, update: dict) -> bool:
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        user = callback.get("from", {}) if callback else message.get("from", {})
        if message.get("chat", {}).get("type") != "private":
            return False
        if not user.get("id") or user.get("is_bot") or message["chat"]["id"] != user["id"]:
            return True
        user_id = user["id"]
        try:
            if callback:
                try:
                    await self.telegram.call(
                        "answerCallbackQuery", callback_query_id=callback["id"]
                    )
                except APIError:
                    pass  # Expired acknowledgement must not bypass authorization below.
                await self.callback(user_id, callback.get("data", ""), message.get("message_id"))
            else:
                await self.command(user_id, message.get("text", "/help"))
        except ManagementError as exc:
            try:
                await self.send(user_id, str(exc))
            except APIError:
                pass
        except APIError as exc:
            log.warning("Management request failed: %s", exc)
            try:
                await self.send(
                    user_id, "Could not verify permissions or contact Telegram. Try again."
                )
            except APIError:
                pass
        return True
