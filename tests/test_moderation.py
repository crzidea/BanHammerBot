import copy
import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from banhammerbot.api import APIError, ImageAsset
from banhammerbot.config import Config
from banhammerbot.decision import Decision, InvalidDecision
from banhammerbot.moderation import Moderator, message_summary
from banhammerbot.storage import Store


def update(number=1, text="Buy discount tokens at spam.example", **fields):
    return {
        "update_id": number,
        "message": {
            "message_id": number,
            "date": int(time.time()),
            "chat": {"id": -1001, "type": "supergroup"},
            "from": {"id": 42, "first_name": "Deals", "username": "buy_deals", "is_bot": False},
            "text": text,
            **fields,
        },
    }


class ModerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config("token", "key", frozenset({-1001}), dry_run=False)
        self.telegram = AsyncMock()
        self.members = ["member"]
        self.bot_id = 9000
        self.bot_member = {
            "status": "administrator",
            "can_restrict_members": True,
            "can_delete_messages": True,
        }
        self.delete_error = False

        async def call(method, **params):
            if method == "getChatMember":
                if params["user_id"] == self.bot_id:
                    return self.bot_member
                status = self.members.pop(0) if len(self.members) > 1 else self.members[0]
                return {"status": status, "can_restrict_members": True}
            if method == "getUserProfilePhotos":
                return {"photos": [[{"file_id": "avatar", "width": 640, "height": 640}]]}
            if method == "getChat":
                return {"id": params["chat_id"], "type": "supergroup", "title": "Test group"}
            if method == "deleteMessage" and self.delete_error:
                raise APIError("telegram", 400)
            return True

        self.telegram.call.side_effect = call
        self.telegram.image.return_value = ImageAsset("jpg", b"\xff\xd8\xffavatar")
        self.jev = AsyncMock()
        self.jev.decide.return_value = Decision(0.99, 0.01, 0.9, "jev-1.14.0")
        self.store = Store(":memory:")
        self.bot = Moderator(self.config, self.telegram, self.jev, self.store, bot_id=self.bot_id)
        self.bot.username = "banhammerbot"

    def tearDown(self):
        self.store.close()

    def action(self, number=1):
        row = self.store.db.execute(
            "SELECT action FROM audit WHERE update_id=?", (number,)
        ).fetchone()
        return row[0] if row else None

    def calls(self, method):
        return [c for c in self.telegram.call.call_args_list if c.args[0] == method]

    async def test_high_confidence_bans_and_deletes(self):
        await self.bot.handle(update())
        self.assertEqual(self.action(), "banned")
        self.assertEqual(
            self.calls("banChatMember")[0].kwargs,
            {
                "chat_id": -1001,
                "user_id": 42,
                "revoke_messages": True,
            },
        )
        self.assertEqual(len(self.calls("deleteMessage")), 1)
        checks = self.calls("getChatMember")
        self.assertEqual(sum(c.kwargs["user_id"] == 42 for c in checks), 2)
        self.assertEqual(sum(c.kwargs["user_id"] == self.bot_id for c in checks), 2)
        evidence, images = self.jev.decide.call_args.args
        self.assertEqual(evidence["sender"]["username"], "buy_deals")
        self.assertIn("sender_avatar", images)

    async def test_dry_run_never_bans_or_deletes(self):
        self.bot.config = replace(self.config, dry_run=True)
        await self.bot.handle(update())
        self.assertEqual(self.action(), "would_ban")
        self.assertFalse(self.calls("banChatMember") + self.calls("deleteMessage"))

    async def test_per_group_dry_run_overrides_global_default(self):
        self.store.set_dry_run(-1001, True)
        await self.bot.handle(update())
        self.assertEqual(self.action(), "would_ban")
        self.bot.config = replace(self.config, dry_run=True)
        self.store.set_dry_run(-1001, False)
        await self.bot.handle(update(2))
        self.assertEqual(self.action(2), "banned")

    async def test_private_management_never_reaches_jev(self):
        self.members = ["administrator"]
        await self.bot.handle(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42},
                    "text": "/select -1001",
                    "message_id": 1,
                },
            }
        )
        self.assertEqual(self.store.selected_group(42), -1001)
        self.jev.decide.assert_not_awaited()

    async def test_limited_group_admin_cannot_unban(self):
        original = self.telegram.call.side_effect

        async def call(method, **params):
            if method == "getChatMember" and params["user_id"] == 42:
                return {"status": "administrator", "can_restrict_members": False}
            return await original(method, **params)

        self.telegram.call.side_effect = call
        await self.bot.handle(update(text="/unban 123"))
        self.assertFalse(self.calls("unbanChatMember"))
        self.jev.decide.assert_not_awaited()

    async def test_all_groups_mode_moderates_new_groups_without_reconfiguration(self):
        self.bot.config = replace(self.config, chat_ids=frozenset())
        for number, chat_id, chat_type in ((1, -1001, "supergroup"), (2, -999, "group")):
            await self.bot.handle(update(number, chat={"id": chat_id, "type": chat_type}))
            self.assertEqual(self.action(number), "banned")
        self.assertEqual([c.kwargs["chat_id"] for c in self.calls("banChatMember")], [-1001, -999])

    async def test_all_groups_mode_still_ignores_private_chats_and_channels(self):
        self.bot.config = replace(self.config, chat_ids=frozenset())
        for number, chat_type in enumerate(("private", "channel"), start=1):
            await self.bot.handle(update(number, chat={"id": -999, "type": chat_type}))
        self.telegram.call.assert_not_awaited()
        self.jev.decide.assert_not_awaited()

    async def test_missing_bot_permissions_prevents_analysis_and_actions(self):
        self.bot.config = replace(self.config, chat_ids=frozenset())
        for number, member in enumerate(
            (
                {"status": "member"},
                {"status": "left"},
                {"status": "kicked"},
                {
                    "status": "administrator",
                    "can_restrict_members": False,
                    "can_delete_messages": True,
                },
                {
                    "status": "administrator",
                    "can_restrict_members": True,
                    "can_delete_messages": False,
                },
                {"status": "administrator"},
            ),
            start=1,
        ):
            self.bot_member = member
            await self.bot.handle(update(number))
            self.assertEqual(self.action(number), "inactive")
        self.jev.decide.assert_not_awaited()
        self.assertFalse(self.calls("getUserProfilePhotos") + self.calls("banChatMember"))

    async def test_bot_permissions_rechecked_after_promotion_and_demotion(self):
        self.bot.config = replace(self.config, chat_ids=frozenset())
        self.jev.decide.return_value = Decision(0, 0, 0, "jev")
        admin = self.bot_member
        self.bot_member = {"status": "member"}
        await self.bot.handle(update(1))
        self.bot_member = admin
        await self.bot.handle(update(2))
        self.bot_member = {"status": "member"}
        await self.bot.handle(update(3))
        self.assertEqual([self.action(i) for i in (1, 2, 3)], ["inactive", "allow", "inactive"])
        self.assertEqual(self.jev.decide.await_count, 1)

    async def test_bot_permissions_lost_during_decision_prevents_ban(self):
        async def decide(*args):
            self.bot_member["can_restrict_members"] = False
            return Decision(1, 1, 1, "jev")

        self.jev.decide.side_effect = decide
        await self.bot.handle(update())
        self.assertEqual(self.action(), "inactive")
        self.assertFalse(self.calls("banChatMember") + self.calls("deleteMessage"))

    async def test_history_is_isolated_between_groups_in_all_groups_mode(self):
        self.bot.config = replace(self.config, chat_ids=frozenset())
        self.jev.decide.return_value = Decision(0, 0, 0, "jev")
        await self.bot.handle(update(1, "hello"))
        await self.bot.handle(update(2, "another group", chat={"id": -1002, "type": "supergroup"}))
        self.assertEqual(self.jev.decide.call_args.args[0]["recent_messages_by_same_sender"], [])

    async def test_allow_review_and_profile_only(self):
        for number, decision, action in (
            (1, Decision(0.01, 0.01, 1, "jev"), "allow"),
            (2, Decision(0.8, 0.01, 1, "jev"), "review"),
            (3, Decision(0.01, 0.98, 0, "jev"), "banned"),
        ):
            self.jev.decide.return_value = decision
            await self.bot.handle(update(number))
            self.assertEqual(self.action(number), action)
        self.assertEqual(len(self.calls("banChatMember")), 1)

    async def test_protected_users_and_chats_are_not_analyzed(self):
        items = [update(1, sender_chat={"id": -1002}), update(2), update(3), update(4)]
        items[1]["message"]["chat"]["id"] = -1002
        items[2]["message"]["chat"]["type"] = "private"
        items[3]["message"]["from"]["is_bot"] = True
        for item in items:
            await self.bot.handle(item)
        self.jev.decide.assert_not_awaited()
        self.telegram.call.assert_not_awaited()

    async def test_admin_and_allowlisted_user_exempt(self):
        self.members = ["administrator"]
        await self.bot.handle(update())
        self.members = ["member"]
        self.bot.config = replace(self.config, exempt_user_ids=frozenset({42}))
        await self.bot.handle(update(2))
        self.jev.decide.assert_not_awaited()

    async def test_admin_promoted_during_decision_not_banned(self):
        self.members = ["member", "administrator"]
        await self.bot.handle(update())
        self.assertEqual(self.action(), "exempt_after_decision")
        self.assertFalse(self.calls("banChatMember"))

    async def test_permission_check_failure_leaves_user_alone(self):
        self.telegram.call.side_effect = APIError("telegram", 403)
        await self.bot.handle(update())
        self.jev.decide.assert_not_awaited()
        self.assertEqual(self.action(), "error")

    async def test_api_failure_and_invalid_decision_do_not_ban(self):
        for number, error in enumerate((APIError("jev", 503), InvalidDecision("bad")), start=1):
            self.jev.decide.side_effect = error
            await self.bot.handle(update(number))
            self.assertEqual(self.action(number), "error")
        self.assertFalse(self.calls("banChatMember"))

    async def test_jev_rate_limit_cooldown(self):
        self.jev.decide.side_effect = APIError("jev", 429, 60)
        await self.bot.handle(update())
        await self.bot.handle(update(2))
        self.assertEqual(self.jev.decide.await_count, 1)
        self.assertEqual(self.action(2), "error")

    async def test_ban_failure_does_not_delete_message(self):
        original = self.telegram.call.side_effect

        async def call(method, **params):
            if method == "banChatMember":
                raise APIError("telegram", 403)
            return await original(method, **params)

        self.telegram.call.side_effect = call
        await self.bot.handle(update())
        self.assertEqual(self.action(), "error")
        self.assertFalse(self.calls("deleteMessage"))

    async def test_deletion_failure_preserves_successful_ban(self):
        self.delete_error = True
        await self.bot.handle(update())
        self.assertEqual(self.action(), "banned")

    async def test_duplicate_update_is_not_applied_twice(self):
        event = update()
        await self.bot.handle(event)
        await self.bot.handle(event)
        self.assertEqual(self.jev.decide.await_count, 1)
        self.assertEqual(len(self.calls("banChatMember")), 1)

    async def test_old_messages_ignored_but_recent_edits_analyzed(self):
        old = update(date=int(time.time()) - 1000)
        await self.bot.handle(old)
        self.assertEqual(self.action(), "stale")
        edited = {
            "update_id": 2,
            "edited_message": {
                **old["message"],
                "edit_date": int(time.time()),
            },
        }
        await self.bot.handle(edited)
        self.assertEqual(self.action(2), "banned")

    async def test_history_is_same_user_same_chat_and_edits_replace_original(self):
        self.jev.decide.return_value = Decision(0, 0, 0, "jev")
        await self.bot.handle(update(1, "hello"))
        await self.bot.handle(update(2, "hello again"))
        previous = self.jev.decide.call_args.args[0]["recent_messages_by_same_sender"]
        self.assertEqual([m["text"] for m in previous], ["hello"])
        edited = update(3, "new text", message_id=2)
        await self.bot.handle(edited)
        previous = self.jev.decide.call_args.args[0]["recent_messages_by_same_sender"]
        self.assertEqual([m["text"] for m in previous], ["hello"])
        other = update(4)
        other["message"]["from"]["id"] = 43
        await self.bot.handle(other)
        self.assertEqual(self.jev.decide.call_args.args[0]["recent_messages_by_same_sender"], [])

    async def test_history_expiration_and_bound(self):
        summary = message_summary(update()["message"])
        self.bot.recent((-1001, 42), summary, 1)
        self.assertEqual(self.bot.recent((-1001, 42), {**summary, "message_id": 2}, 1000), [])
        for user_id in range(300):
            self.bot.recent((-1001, user_id), summary, 1000)
        self.assertEqual(len(self.bot.history), 256)

    async def test_avatar_is_cached_and_message_photo_is_included(self):
        self.jev.decide.return_value = Decision(0, 0, 0, "jev")
        await self.bot.handle(update())
        await self.bot.handle(update(2, photo=[{"file_id": "photo", "width": 640, "height": 640}]))
        self.assertEqual(len(self.calls("getUserProfilePhotos")), 1)
        self.assertIn("current_message_image", self.jev.decide.call_args.args[1])

    async def test_unavailable_avatar_is_explicit_and_text_still_analyzed(self):
        self.telegram.image.side_effect = APIError("telegram image")
        await self.bot.handle(update())
        evidence, images = self.jev.decide.call_args.args
        self.assertEqual(evidence["image_status"]["avatar"], "unavailable")
        self.assertEqual(images, {})

    async def test_no_avatar_supported(self):
        original = self.telegram.call.side_effect

        async def call(method, **params):
            return (
                {"photos": []}
                if method == "getUserProfilePhotos"
                else await original(method, **params)
            )

        self.telegram.call.side_effect = call
        await self.bot.handle(update())
        self.assertEqual(self.jev.decide.call_args.args[0]["image_status"]["avatar"], "not_visible")

    async def test_admin_unban_only_if_banned_and_dry_run(self):
        self.members = ["administrator"]
        await self.bot.handle(update(text="/unban@BanHammerBot 123"))
        self.assertEqual(
            self.calls("unbanChatMember")[0].kwargs,
            {
                "chat_id": -1001,
                "user_id": 123,
                "only_if_banned": True,
            },
        )
        self.bot.config = replace(self.config, dry_run=True)
        await self.bot.handle(update(2, text="/unban 123"))
        self.assertEqual(len(self.calls("unbanChatMember")), 1)
        self.jev.decide.assert_not_awaited()

    async def test_regular_user_cannot_unban_or_bypass_analysis_using_command(self):
        await self.bot.handle(update(text="/unban 123"))
        self.assertFalse(self.calls("unbanChatMember"))
        self.jev.decide.assert_awaited_once()

    async def test_service_messages_ignored(self):
        event = update()
        del event["message"]["text"]
        event["message"]["new_chat_members"] = [{"id": 42}]
        await self.bot.handle(event)
        self.telegram.call.assert_not_awaited()

    def test_utf16_and_hidden_links_extracted_without_mutation(self):
        message = update(text="😀 https://example.com click")["message"]
        message["entities"] = [
            {"type": "url", "offset": 3, "length": 19},
            {"type": "text_link", "offset": 23, "length": 5, "url": "https://hidden.example"},
        ]
        original = copy.deepcopy(message)
        self.assertEqual(
            message_summary(message)["links"],
            [
                "https://example.com",
                "https://hidden.example",
            ],
        )
        self.assertEqual(message, original)
