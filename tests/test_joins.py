import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from banhammerbot.api import APIError
from banhammerbot.config import Config
from banhammerbot.joins import JoinMute
from banhammerbot.storage import Store


def joined(user_id=42, date=1000, old=None, new=None):
    return {
        "update_id": 1,
        "chat_member": {
            "chat": {"id": -1001, "type": "supergroup", "title": "Test group"},
            "date": date,
            "old_chat_member": old or {"status": "left", "user": {"id": user_id}},
            "new_chat_member": new or {"status": "member", "user": {"id": user_id}},
        },
    }


class JoinMuteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.set_join_mute(-1001, 600)
        self.config = Config("token", "key", dry_run=False)
        self.telegram = AsyncMock()
        self.bot_member = {
            "status": "administrator",
            "can_restrict_members": True,
            "can_delete_messages": True,
        }
        self.user_member = {"status": "member"}

        async def call(method, **params):
            if method == "getChatMember":
                return self.bot_member if params["user_id"] == 9000 else self.user_member
            return True

        self.telegram.call.side_effect = call
        self.joins = JoinMute(self.config, self.telegram, self.store, 9000)
        self.clock = patch("banhammerbot.joins.time.time", return_value=1000)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.store.close()

    def calls(self, method):
        return [c for c in self.telegram.call.call_args_list if c.args[0] == method]

    async def test_new_member_muted_until_join_time_plus_duration(self):
        await self.joins.handle(joined())
        call = self.calls("restrictChatMember")[0]
        self.assertEqual(call.kwargs["until_date"], 1600)
        self.assertEqual(call.kwargs["user_id"], 42)
        self.assertEqual(call.kwargs["chat_id"], -1001)
        self.assertTrue(call.kwargs["use_independent_chat_permissions"])
        self.assertFalse(any(call.kwargs["permissions"].values()))
        self.assertTrue(self.store.join_handled(-1001, 42, 1000))

    async def test_dry_run_logs_without_restricting_and_group_override_enables(self):
        self.joins.config = replace(self.config, dry_run=True)
        await self.joins.handle(joined())
        self.assertFalse(self.calls("restrictChatMember"))
        row = self.store.db.execute("SELECT action FROM join_actions").fetchone()
        self.assertEqual(row[0], "would_mute")
        self.store.set_dry_run(-1001, False)
        await self.joins.handle(joined(user_id=43))
        self.assertEqual(len(self.calls("restrictChatMember")), 1)

    async def test_disabled_policy_and_other_groups_are_not_muted(self):
        self.store.set_join_mute(-1001, 0)
        await self.joins.handle(joined())
        event = joined()
        event["chat_member"]["chat"]["id"] = -1002
        await self.joins.handle(event)
        self.telegram.call.assert_not_awaited()

    async def test_duplicate_join_service_and_member_updates_are_not_reapplied(self):
        await self.joins.handle(joined())
        await self.joins.handle(joined())
        await self.joins.handle(
            {
                "update_id": 2,
                "message": {
                    "chat": {"id": -1001, "type": "supergroup"},
                    "date": 1000,
                    "new_chat_members": [{"id": 42}],
                },
            }
        )
        self.assertEqual(len(self.calls("restrictChatMember")), 1)

    async def test_service_join_with_multiple_members(self):
        await self.joins.handle(
            {
                "update_id": 2,
                "message": {
                    "chat": {"id": -1001, "type": "supergroup"},
                    "date": 1000,
                    "new_chat_members": [{"id": 42}, {"id": 43}, {"id": 44, "is_bot": True}],
                },
            }
        )
        self.assertEqual([c.kwargs["user_id"] for c in self.calls("restrictChatMember")], [42, 43])

    async def test_bots_and_exempt_users_are_ignored(self):
        self.joins.config = replace(self.config, exempt_user_ids=frozenset({42}))
        await self.joins.handle(joined())
        await self.joins.handle(
            joined(new={"status": "member", "user": {"id": 43, "is_bot": True}})
        )
        self.telegram.call.assert_not_awaited()

    async def test_admins_departed_and_already_restricted_members_not_overwritten(self):
        for status in ("administrator", "creator", "restricted", "left", "kicked"):
            self.user_member = {"status": status}
            await self.joins.handle(joined())
        self.assertFalse(self.calls("restrictChatMember"))

    async def test_permission_changes_are_not_mistaken_for_joins(self):
        for old in (
            {"status": "member"},
            {"status": "administrator"},
            {"status": "restricted", "is_member": True},
        ):
            await self.joins.handle(joined(old=old))
        self.telegram.call.assert_not_awaited()
        await self.joins.handle(joined(old={"status": "restricted", "is_member": False}))
        self.assertEqual(len(self.calls("restrictChatMember")), 1)

    async def test_basic_groups_and_groups_outside_scope_are_ignored(self):
        event = joined()
        event["chat_member"]["chat"]["type"] = "group"
        await self.joins.handle(event)
        self.joins.config = replace(self.config, chat_ids=frozenset({-1002}))
        await self.joins.handle(joined())
        self.telegram.call.assert_not_awaited()

    async def test_missing_bot_permissions_prevents_mute(self):
        self.bot_member["can_restrict_members"] = False
        await self.joins.handle(joined())
        self.assertFalse(self.calls("restrictChatMember"))

    async def test_stale_and_nearly_expired_deadlines_are_skipped(self):
        await self.joins.handle(joined(date=500))
        self.store.set_join_mute(-1001, 60)
        await self.joins.handle(joined(date=970))
        self.telegram.call.assert_not_awaited()

    async def test_deadline_is_rechecked_after_permission_queries(self):
        self.store.set_join_mute(-1001, 60)
        with patch("banhammerbot.joins.time.time", side_effect=[1000, 1050]):
            await self.joins.handle(joined())
        self.assertFalse(self.calls("restrictChatMember"))

    async def test_api_failure_is_audited_without_stopping_worker(self):
        self.telegram.call.side_effect = APIError("telegram", 403)
        with self.assertLogs("banhammerbot.joins", level="WARNING"):
            await self.joins.handle(joined())
        self.assertEqual(
            self.store.db.execute("SELECT action FROM join_actions").fetchone()[0], "error"
        )

    async def test_bot_membership_events_register_group_without_muting(self):
        event = joined()
        event["my_chat_member"] = event.pop("chat_member")
        await self.joins.handle(event)
        self.assertEqual(self.store.groups(), [-1001])
        self.telegram.call.assert_not_awaited()
