import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import AsyncMock

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
            {"select", "dryrun", "joinmute", "menu"}
            <= {command["command"] for command in commands[0].kwargs["commands"]}
        )
        self.assertEqual(commands[1].kwargs["scope"], {"type": "all_chat_administrators"})
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
