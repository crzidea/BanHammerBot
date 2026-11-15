import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from banhammerbot.api import APIError
from banhammerbot.config import Config
from banhammerbot.management import Management, ManagementError, duration
from banhammerbot.storage import Store


def dm(text, user_id=42):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "text": text,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id},
        },
    }


def callback(data, user_id=42):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "callback-id",
            "data": data,
            "from": {"id": user_id},
            "message": {"message_id": 10, "chat": {"id": user_id, "type": "private"}},
        },
    }


class SettingsTests(unittest.TestCase):
    def test_duration_bounds(self):
        for text, expected in (
            ("off", 0),
            ("60s", 60),
            ("10m", 600),
            ("2h", 7200),
            ("1d", 86400),
            ("30d", 2592000),
            ("60", 60),
        ):
            self.assertEqual(duration(text), expected)
        for value in ("0", "30s", "-60", "forever", "31d", "1.5h", "nan", "", "999999999999h"):
            with self.subTest(value=value), self.assertRaises(ManagementError):
                duration(value)

    def test_settings_selection_and_join_log_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/state.sqlite3"
            store = Store(path)
            store.remember_group({"id": -1, "title": "Group", "type": "supergroup"})
            store.set_dry_run(-1, False)
            store.set_join_mute(-1, 600)
            store.select_group(42, -1)
            store.record_join(-1, 50, 1000, "muted", 1600)
            store.close()
            store = Store(path)
            try:
                self.assertFalse(store.settings(-1, True).dry_run)
                self.assertEqual(store.settings(-1, True).join_mute_seconds, 600)
                self.assertEqual(store.selected_group(42), -1)
                self.assertTrue(store.join_handled(-1, 50, 1000))
                self.assertTrue(store.settings(-2, True).dry_run)
                self.assertEqual(store.settings(-2, True).join_mute_seconds, 0)
            finally:
                store.close()

    def test_join_mute_does_not_override_default_dry_run(self):
        store = Store(":memory:")
        try:
            store.set_join_mute(-1, 600)
            self.assertTrue(store.settings(-1, True).dry_run)
            self.assertFalse(store.settings(-1, False).dry_run)
            with self.assertRaises(sqlite3.IntegrityError):
                store.set_join_mute(-1, 30)
        finally:
            store.close()

    def test_disabled_mute_remembers_duration_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/state.sqlite3"
            with closing(Store(path)) as store:
                store.set_join_mute(-1001, 43200)
                store.set_join_mute(-1001, 0)
            with closing(Store(path)) as store:
                settings = store.settings(-1001, True)
                self.assertEqual(settings.join_mute_seconds, 0)
                self.assertEqual(settings.join_mute_duration, 43200)

    def test_mute_duration_migration_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/old.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("""
                    CREATE TABLE group_settings (
                        chat_id INTEGER PRIMARY KEY, dry_run INTEGER,
                        join_mute_seconds INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO group_settings VALUES(-1001, 0, 43200);
                    INSERT INTO group_settings VALUES(-1002, NULL, 0);
                """)
            for _ in range(2):
                with closing(Store(path)) as store:
                    active = store.settings(-1001, True)
                    self.assertFalse(active.dry_run)
                    self.assertEqual(active.join_mute_seconds, 43200)
                    self.assertEqual(active.join_mute_duration, 43200)
                    disabled = store.settings(-1002, True)
                    self.assertTrue(disabled.dry_run)
                    self.assertFalse(store.settings(-1002, False).dry_run)
                    self.assertEqual(disabled.join_mute_seconds, 0)
                    self.assertEqual(disabled.join_mute_duration, 14400)

    def test_existing_database_migrates_without_losing_audit_or_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/old.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("""
                    CREATE TABLE cursor (id INTEGER PRIMARY KEY, offset INTEGER);
                    INSERT INTO cursor VALUES(1, 100);
                    CREATE TABLE audit (update_id INTEGER PRIMARY KEY, created_at TEXT,
                        chat_id INTEGER, user_id INTEGER, message_id INTEGER, action TEXT,
                        decision TEXT, detail TEXT);
                    INSERT INTO audit VALUES (99, 'old', -1001, 42, 1, 'allow', NULL, NULL);
                """)
            store = Store(path)
            try:
                self.assertEqual(store.offset(), 100)
                self.assertTrue(store.completed(99))
                self.assertEqual(store.groups(), [-1001])
                store.set_dry_run(-1001, False)
            finally:
                store.close()


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.config = Config("token", "key")
        self.telegram = AsyncMock()
        self.chats = {
            -1001: {"id": -1001, "title": "First group", "type": "supergroup"},
            -1002: {"id": -1002, "title": "Secret group", "type": "supergroup"},
        }
        self.roles = {}
        for chat in self.chats.values():
            self.store.remember_group(chat)
            self.roles[(chat["id"], 42)] = {"status": "administrator", "can_restrict_members": True}
            self.roles[(chat["id"], 9000)] = {
                "status": "administrator",
                "can_restrict_members": True,
                "can_delete_messages": True,
            }

        async def call(method, **params):
            if method == "getChatMember":
                return self.roles.get((params["chat_id"], params["user_id"]), {"status": "member"})
            if method == "getChat":
                return self.chats[params["chat_id"]]
            return True

        self.telegram.call.side_effect = call
        self.manager = Management(self.config, self.telegram, self.store, 9000)
        self.manager.username = "test_bot"

    def tearDown(self):
        self.store.close()

    def calls(self, method):
        return [c for c in self.telegram.call.call_args_list if c.args[0] == method]

    async def select(self, chat=-1001):
        await self.manager.handle(dm(f"/select {chat}"))

    async def test_registers_private_and_admin_commands_and_menu(self):
        await self.manager.register()
        commands = self.calls("setMyCommands")
        self.assertEqual(commands[0].kwargs["scope"], {"type": "all_private_chats"})
        self.assertTrue(
            {"select", "dryrun", "joinmute", "menu", "banlog"}
            <= {command["command"] for command in commands[0].kwargs["commands"]}
        )
        self.assertEqual(commands[1].kwargs["scope"], {"type": "all_chat_administrators"})
        self.assertIn("banlog", {command["command"] for command in commands[1].kwargs["commands"]})
        self.assertEqual(
            self.calls("setChatMenuButton")[0].kwargs["menu_button"], {"type": "commands"}
        )

    async def test_group_selection_and_per_group_dry_run(self):
        await self.select()
        await self.manager.handle(dm("/dryrun off"))
        self.assertEqual(self.store.selected_group(42), -1001)
        self.assertFalse(self.store.settings(-1001, True).dry_run)
        self.assertTrue(self.store.settings(-1002, True).dry_run)
        await self.manager.handle(dm("/dryrun on"))
        self.assertTrue(self.store.settings(-1001, False).dry_run)

    async def test_joinmute_settings_and_off_are_group_specific(self):
        await self.select()
        await self.manager.handle(dm("/joinmute 10m"))
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 600)
        self.assertEqual(self.store.settings(-1002, True).join_mute_seconds, 0)
        await self.manager.handle(dm("/joinmute off"))
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 0)
        await self.manager.handle(dm("/joinmute on"))
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 600)
        self.assertEqual(self.store.settings(-1002, True).join_mute_seconds, 0)

    async def test_mute_toggle_uses_default_then_restores_saved_duration_in_target_group(self):
        await self.select(-1002)
        await self.manager.handle(callback("mute:-1001:1"))
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 14400)
        await self.manager.handle(callback("join:-1001:43200"))
        await self.manager.handle(callback("mute:-1001:0"))
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 0)
        await self.manager.handle(callback("mute:-1001:1"))
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 43200)
        self.assertEqual(self.store.settings(-1002, True).join_mute_seconds, 0)

    async def test_revoked_admin_cannot_toggle_mute(self):
        self.store.set_join_mute(-1001, 43200)
        self.roles[(-1001, 42)] = {"status": "member"}
        await self.manager.handle(callback("mute:-1001:0"))
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 43200)
        self.store.set_join_mute(-1001, 0)
        await self.manager.handle(callback("mute:-1001:1"))
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 0)

    async def test_unauthorized_selection_does_not_reveal_title_or_save_selection(self):
        self.roles[(-1002, 42)] = {"status": "member"}
        await self.select(-1002)
        self.assertIsNone(self.store.selected_group(42))
        self.assertFalse(self.calls("getChat"))
        self.assertNotIn("Secret group", str(self.calls("sendMessage")))

    async def test_group_list_only_displays_authorized_groups(self):
        self.roles[(-1002, 42)] = {"status": "member"}
        await self.manager.handle(dm("/select"))
        messages = str(self.calls("sendMessage"))
        self.assertIn("First group", messages)
        self.assertNotIn("Secret group", messages)

    async def test_revoked_admin_cannot_change_settings_or_use_old_buttons(self):
        await self.select()
        self.roles[(-1001, 42)] = {"status": "member"}
        await self.manager.handle(dm("/dryrun off"))
        await self.manager.handle(callback("join:-1001:600"))
        self.assertTrue(self.store.settings(-1001, True).dry_run)
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 0)
        self.assertEqual(len(self.calls("answerCallbackQuery")), 1)

    async def test_old_menu_changes_its_explicit_group_not_new_selection(self):
        await self.select(-1001)
        await self.select(-1002)
        await self.manager.handle(callback("dry:-1001:0"))
        self.assertFalse(self.store.settings(-1001, True).dry_run)
        self.assertTrue(self.store.settings(-1002, True).dry_run)
        self.assertEqual(self.store.selected_group(42), -1001)

    async def test_hour_presets_apply_to_target_group_and_edit_existing_menu(self):
        await self.select()
        rows = self.calls("sendMessage")[-1].kwargs["reply_markup"]["inline_keyboard"]
        buttons = {button["text"]: button["callback_data"] for row in rows for button in row}
        sent_count = len(self.calls("sendMessage"))
        for label, seconds in (("4 hours", 14400), ("12 hours", 43200), ("24 hours", 86400)):
            await self.manager.handle(callback(buttons[label]))
            self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, seconds)
            self.assertEqual(self.store.settings(-1002, True).join_mute_seconds, 0)
            edited = self.calls("editMessageText")[-1].kwargs
            self.assertEqual((edited["chat_id"], edited["message_id"]), (42, 10))
        self.assertEqual(len(self.calls("sendMessage")), sent_count)

    async def test_unchanged_menu_refresh_does_not_send_an_error(self):
        original = self.telegram.call.side_effect

        async def call(method, **params):
            if method == "editMessageText":
                raise APIError("telegram", 400, description="Bad Request: message is not modified")
            return await original(method, **params)

        self.telegram.call.side_effect = call
        await self.manager.handle(callback("menu:-1001"))
        self.assertEqual(len(self.calls("editMessageText")), 1)
        self.assertFalse(self.calls("sendMessage"))

    async def test_deleted_menu_is_replaced(self):
        original = self.telegram.call.side_effect

        async def call(method, **params):
            if method == "editMessageText":
                raise APIError(
                    "telegram", 400, description="Bad Request: message to edit not found"
                )
            return await original(method, **params)

        self.telegram.call.side_effect = call
        await self.manager.handle(callback("menu:-1001"))
        self.assertEqual(len(self.calls("sendMessage")), 1)
        self.assertIn("inline_keyboard", self.calls("sendMessage")[0].kwargs["reply_markup"])

    async def test_group_picker_reuses_menu_message(self):
        await self.manager.handle(callback("groups:0"))
        self.assertEqual(self.calls("editMessageText")[0].kwargs["message_id"], 10)
        self.assertFalse(self.calls("sendMessage"))

    async def test_callback_does_not_trust_someone_elses_selection(self):
        await self.select()
        await self.manager.handle(callback("dry:-1001:0", user_id=43))
        self.assertTrue(self.store.settings(-1001, True).dry_run)
        self.assertIsNone(self.store.selected_group(43))

    async def test_limited_admin_cannot_manage_but_owner_can(self):
        self.roles[(-1001, 42)] = {"status": "administrator", "can_restrict_members": False}
        await self.select()
        self.assertIsNone(self.store.selected_group(42))
        self.roles[(-1001, 42)] = {"status": "creator"}
        await self.select()
        self.assertEqual(self.store.selected_group(42), -1001)

    async def test_bot_permissions_and_scope_are_enforced(self):
        self.roles[(-1001, 9000)] = {"status": "member"}
        await self.select()
        self.assertIsNone(self.store.selected_group(42))
        self.manager.config = replace(self.config, chat_ids=frozenset({-1001}))
        await self.select(-1002)
        self.assertIsNone(self.store.selected_group(42))

    async def test_no_selection_prompts_and_invalid_arguments_do_not_change_settings(self):
        await self.manager.handle(dm("/dryrun off"))
        self.assertIn("Select a group first", self.calls("sendMessage")[-1].kwargs["text"])
        await self.select()
        for text in ("/dryrun false", "/joinmute 20s", "/joinmute 1d extra", "/select wrong"):
            await self.manager.handle(dm(text))
        self.assertTrue(self.store.settings(-1001, True).dry_run)
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 0)

    async def test_invalid_callbacks_do_not_apply_settings(self):
        for data in (
            "dry:-1001:5",
            "mute:-1001:5",
            "mute:-1001",
            "join:-1001:30",
            "join:-1001:99999999",
            "select:-1001:0",
            "oops",
        ):
            await self.manager.handle(callback(data))
        self.assertTrue(self.store.settings(-1001, True).dry_run)
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 0)

    async def test_joinmute_rejects_basic_groups(self):
        self.chats[-1001]["type"] = "group"
        await self.select()
        await self.manager.handle(dm("/joinmute 10m"))
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 0)
        self.assertIn("supergroup", self.calls("sendMessage")[-1].kwargs["text"])
        await self.manager.handle(callback("mute:-1001:1"))
        self.assertEqual(self.store.settings(-1001, True).join_mute_seconds, 0)

    async def test_unban_obeys_selected_group_dry_run(self):
        await self.select()
        await self.manager.handle(dm("/unban 123"))
        self.assertFalse(self.calls("unbanChatMember"))
        await self.manager.handle(dm("/dryrun off"))
        await self.manager.handle(dm("/unban 123"))
        self.assertEqual(
            self.calls("unbanChatMember")[0].kwargs,
            {"chat_id": -1001, "user_id": 123, "only_if_banned": True},
        )

    def record_ban(self, number=1, chat_id=-1001, action="banned", decision=None):
        self.store.record(
            number,
            {
                "chat": {"id": chat_id},
                "from": {"id": 8745238344},
                "message_id": 26699 + number,
                "text": "private message content",
            },
            action,
            decision,
        )

    async def test_banlog_shows_bans_and_dry_runs_in_either_mode(self):
        self.roles[(-1001, 8745238344)] = {
            "status": "kicked",
            "user": {"id": 8745238344, "first_name": "测试用户", "last_name": "<b>& Name"},
        }
        self.record_ban(1, action="banned", decision={"unsolicited_ad": 0.99, "spam": 0.01})
        self.record_ban(2, action="would_ban", decision={"unsolicited_ad": 0.02, "spam": 0.87})
        self.record_ban(3, action="allow")
        self.record_ban(4, chat_id=-1002)
        await self.select()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                self.store.set_dry_run(-1001, dry_run)
                await self.manager.handle(dm("/banlog@test_bot"))
                sent = self.calls("sendMessage")[-1].kwargs
                self.assertEqual(sent["chat_id"], 42)
                self.assertEqual(sent["parse_mode"], "HTML")
                self.assertEqual(sent["link_preview_options"], {"is_disabled": True})
                text = sent["text"]
                self.assertIn("Dry run: would ban (no action)", text)
                self.assertIn("Banned", text)
                self.assertIn("UTC", text)
                self.assertIn('href="tg://user?id=8745238344"', text)
                self.assertIn(
                    '<a href="tg://user?id=8745238344">测试用户 &lt;b&gt;&amp; Name</a>', text
                )
                self.assertNotIn(">8745238344</a>", text)
                self.assertIn("https://t.me/c/1/26700", text)
                self.assertIn("Violation: 0.99", text)
                self.assertIn("Violation: 0.87", text)
                self.assertNotIn("26702", text)
                self.assertNotIn("26703", text)
                self.assertNotIn("private message content", text)
        self.assertFalse(self.calls("banChatMember") + self.calls("deleteMessage"))
        lookups = [c for c in self.calls("getChatMember") if c.kwargs["user_id"] == 8745238344]
        # One lookup per page, even with two entries for this user.
        self.assertEqual(len(lookups), 2)
        self.assertNotIn("测试用户", "\n".join(self.store.db.iterdump()))

    async def test_banlog_refresh_uses_current_name_without_last_name(self):
        self.record_ban()
        user = {"id": 8745238344, "first_name": "Original name"}
        self.roles[(-1001, 8745238344)] = {"status": "kicked", "user": user}
        await self.manager.handle(dm("/start banlog_-1001"))
        self.assertIn(">Original name</a>", self.calls("sendMessage")[-1].kwargs["text"])
        user["first_name"] = "Updated name"
        await self.manager.handle(callback("banlog:-1001:0"))
        self.assertIn(">Updated name</a>", self.calls("editMessageText")[-1].kwargs["text"])

    async def test_banlog_name_lookup_failure_or_timeout_falls_back_to_id(self):
        self.record_ban()
        original = self.telegram.call.side_effect
        cancelled = asyncio.Event()

        for failure in ("api", "timeout"):
            with self.subTest(failure=failure):

                async def call(method, failure=failure, **params):
                    if method == "getChatMember" and params["user_id"] == 8745238344:
                        if failure == "api":
                            raise APIError("telegram", 400)
                        try:
                            await asyncio.Future()
                        finally:
                            cancelled.set()
                    return await original(method, **params)

                self.telegram.call.side_effect = call
                with patch("banhammerbot.management.BAN_LOG_NAME_TIMEOUT", 0.01):
                    await self.manager.handle(dm("/start banlog_-1001"))
                text = self.calls("sendMessage")[-1].kwargs["text"]
                self.assertIn('<a href="tg://user?id=8745238344">8745238344</a>', text)
                self.assertIn("<b>Banned</b>", text)
        self.assertTrue(cancelled.is_set())

    async def test_banlog_name_lookup_only_resolves_visible_users_concurrently(self):
        for number in range(1, 7):
            self.store.record(
                number,
                {"chat": {"id": -1001}, "from": {"id": 100 + number}, "message_id": number},
                "banned",
            )
        original = self.telegram.call.side_effect
        started = set()
        all_started = asyncio.Event()

        async def call(method, **params):
            if method == "getChatMember" and 101 <= params["user_id"] <= 106:
                started.add(params["user_id"])
                if len(started) == 5:
                    all_started.set()
                await all_started.wait()
                return {"user": {"id": params["user_id"], "first_name": "Visible user"}}
            return await original(method, **params)

        self.telegram.call.side_effect = call
        await asyncio.wait_for(self.manager.handle(dm("/start banlog_-1001")), timeout=1)
        self.assertEqual(started, {102, 103, 104, 105, 106})
        self.assertEqual(self.calls("sendMessage")[-1].kwargs["text"].count(">Visible user</a>"), 5)

    async def test_banlog_name_lookup_preserves_shutdown_cancellation(self):
        self.record_ban()
        original = self.telegram.call.side_effect
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def call(method, **params):
            if method == "getChatMember" and params["user_id"] == 8745238344:
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return await original(method, **params)

        self.telegram.call.side_effect = call
        task = asyncio.create_task(self.manager.handle(dm("/start banlog_-1001")))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.calls("sendMessage"))

    async def test_banlog_menu_paginates_and_preserves_target_after_group_switch(self):
        for number in range(1, 8):
            self.record_ban(number)
        await self.select()
        rows = self.calls("sendMessage")[-1].kwargs["reply_markup"]["inline_keyboard"]
        button = next(
            button for row in rows for button in row if button["text"] == "Recent ban log"
        )
        await self.select(-1002)
        await self.manager.handle(callback(button["callback_data"]))
        newest = self.calls("editMessageText")[-1].kwargs
        self.assertEqual((newest["chat_id"], newest["message_id"]), (42, 10))
        self.assertIn("First group", newest["text"])
        self.assertEqual(newest["text"].count("<b>Banned</b>"), 5)
        self.assertNotIn("/26701", newest["text"])
        buttons = {
            b["text"]: b["callback_data"]
            for r in newest["reply_markup"]["inline_keyboard"]
            for b in r
        }
        self.assertNotIn("Newer", buttons)
        await self.manager.handle(callback(buttons["Older"]))
        older = self.calls("editMessageText")[-1].kwargs
        self.assertEqual(older["text"].count("<b>Banned</b>"), 2)
        self.assertIn("/26700", older["text"])
        self.assertIn("/26701", older["text"])
        self.assertNotIn("/26702", older["text"])
        buttons = {
            b["text"]: b["callback_data"]
            for r in older["reply_markup"]["inline_keyboard"]
            for b in r
        }
        self.assertNotIn("Older", buttons)
        self.assertIn("Newer", buttons)
        await self.manager.handle(callback(buttons["Back to menu"]))
        self.assertIn("Group settings", self.calls("editMessageText")[-1].kwargs["text"])
        self.assertEqual(self.store.selected_group(42), -1001)

    async def test_banlog_rechecks_admin_permissions_for_command_deep_link_and_pages(self):
        self.record_ban()
        await self.select()
        self.roles[(-1001, 42)] = {"status": "member"}
        with patch.object(self.store, "recent_bans") as query:
            for event in (dm("/banlog"), dm("/start banlog_-1001"), callback("banlog:-1001:5")):
                self.telegram.call.reset_mock()
                await self.manager.handle(event)
                query.assert_not_called()
                self.assertFalse(self.calls("getChat") + self.calls("editMessageText"))
                self.assertNotIn("8745238344", self.calls("sendMessage")[-1].kwargs["text"])

    async def test_banlog_rejects_other_groups_limited_admins_and_missing_bot_permissions(self):
        self.record_ban(chat_id=-1002)
        for role in (
            {"status": "member"},
            {"status": "administrator", "can_restrict_members": False},
        ):
            self.roles[(-1002, 42)] = role
            with patch.object(self.store, "recent_bans") as query:
                await self.manager.handle(callback("banlog:-1002:0"))
                query.assert_not_called()
        self.roles[(-1001, 9000)] = {"status": "member"}
        with patch.object(self.store, "recent_bans") as query:
            await self.manager.handle(dm("/start banlog_-1001"))
            query.assert_not_called()
        self.manager.config = replace(self.config, chat_ids=frozenset({-1001}))
        with patch.object(self.store, "recent_bans") as query:
            await self.manager.handle(callback("banlog:-1002:0"))
            query.assert_not_called()

    async def test_banlog_permission_lookup_failure_does_not_expose_audit(self):
        self.store.select_group(42, -1001)
        self.record_ban()
        original = self.telegram.call.side_effect

        async def call(method, **params):
            if method == "getChatMember":
                raise APIError("telegram", 403)
            return await original(method, **params)

        self.telegram.call.side_effect = call
        with patch.object(self.store, "recent_bans") as query:
            with self.assertLogs("banhammerbot.management", level="WARNING"):
                await self.manager.handle(dm("/banlog"))
            query.assert_not_called()

    async def test_banlog_deep_link_selects_group_and_escapes_title(self):
        self.chats[-1001]["title"] = 'Group <b>& "name"'
        self.record_ban()
        await self.manager.handle(dm("/start banlog_-1001"))
        self.assertEqual(self.store.selected_group(42), -1001)
        text = self.calls("sendMessage")[-1].kwargs["text"]
        self.assertIn("Group &lt;b&gt;&amp; &quot;name&quot;", text)
        self.assertIn("<b>Banned</b>", text)
        self.assertNotIn("Violation:", text)

    async def test_banlog_empty_state_no_selection_and_invalid_requests(self):
        await self.manager.handle(dm("/banlog"))
        self.assertIn("Select a group first", self.calls("sendMessage")[-1].kwargs["text"])
        await self.select()
        await self.manager.handle(dm("/banlog"))
        self.assertIn(
            "No bans or dry-run ban decisions", self.calls("sendMessage")[-1].kwargs["text"]
        )
        for event in (dm("/banlog -1002"), callback("banlog:-1001:-5"), callback("banlog:-1001")):
            with patch.object(self.store, "recent_bans") as query:
                await self.manager.handle(event)
                query.assert_not_called()
        await self.manager.handle(dm("/banlog@different_bot"))
        self.assertEqual(self.store.selected_group(42), -1001)

    async def test_basic_group_banlog_has_user_link_without_invalid_message_link(self):
        self.chats[-1001]["type"] = "group"
        self.record_ban()
        await self.manager.handle(dm("/start banlog_-1001"))
        text = self.calls("sendMessage")[-1].kwargs["text"]
        self.assertIn("tg://user?id=8745238344", text)
        self.assertIn("Message: 26700", text)
        self.assertNotIn("https://t.me/c/", text)

    async def test_group_deep_link_selects_only_with_permission(self):
        await self.manager.handle(dm("/start group_-1001"))
        self.assertEqual(self.store.selected_group(42), -1001)
        await self.manager.handle(dm("/start group_-1001", user_id=43))
        self.assertIsNone(self.store.selected_group(43))

    async def test_private_edits_and_cross_chat_callbacks_are_ignored(self):
        edited = {"update_id": 1, "edited_message": dm("/select -1001")["message"]}
        await self.manager.handle(edited)
        event = callback("dry:-1001:0")
        event["callback_query"]["message"]["chat"]["id"] = 43
        await self.manager.handle(event)
        self.telegram.call.assert_not_awaited()

    async def test_lookup_failure_does_not_change_settings(self):
        self.store.select_group(42, -1001)
        original = self.telegram.call.side_effect

        async def call(method, **params):
            if method == "getChatMember":
                raise APIError("telegram", 403)
            return await original(method, **params)

        self.telegram.call.side_effect = call
        with self.assertLogs("banhammerbot.management", level="WARNING"):
            await self.manager.handle(dm("/dryrun off"))
        self.assertTrue(self.store.settings(-1001, True).dry_run)

    async def test_blocked_private_reply_does_not_stop_worker(self):
        self.telegram.call.side_effect = APIError("telegram", 403)
        self.assertTrue(await self.manager.handle(dm("/dryrun off")))
