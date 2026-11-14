import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from banhammerbot.__main__ import poll, preflight
from banhammerbot.api import APIError
from banhammerbot.config import Config
from banhammerbot.storage import Store


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("secret-telegram", "secret-jev", frozenset({-1001}))

    def test_credentials_not_in_repr(self):
        self.assertNotIn("secret", repr(self.config))

    def test_invalid_configuration(self):
        for changes in (
            {"chat_ids": frozenset()},
            {"chat_ids": frozenset({123})},
            {"ban_threshold": float("nan")},
            {"ban_threshold": 1.1},
            {"review_threshold": 0.9, "ban_threshold": 0.8},
            {"api_timeout": float("inf")},
            {"max_event_age": 0},
            {"api_url": "http://example.com"},
            {"api_url": "https://user:pass@example.com"},
            {"history_limit": 1000},
            {"telegram_token": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.config, **changes)

    def test_environment_dry_run_defaults_and_strict_boolean(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "TYPESAFE_API_KEY": "key",
            "TELEGRAM_CHAT_IDS": "-1001,-2",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
            self.assertTrue(config.dry_run)
            self.assertEqual(config.chat_ids, frozenset({-1001, -2}))
        with patch.dict(os.environ, {**env, "DRY_RUN": "flase"}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_requires_permissions_and_rejects_webhook(self):
        config = Config("token", "key", frozenset({-1001}))
        for webhook, permissions, valid in (
            ("", True, True),
            ("https://example.test", True, False),
            ("", False, False),
        ):
            telegram = AsyncMock()

            async def call(method, webhook=webhook, permissions=permissions, **params):
                return {
                    "getMe": {"id": 12, "username": "bot"},
                    "getWebhookInfo": {"url": webhook},
                    "getChat": {"type": "supergroup"},
                    "getChatMember": {
                        "status": "administrator",
                        "can_restrict_members": permissions,
                        "can_delete_messages": permissions,
                    },
                }[method]

            telegram.call.side_effect = call
            if valid:
                self.assertEqual(await preflight(telegram, config), "bot")
            else:
                with self.assertRaises(ValueError):
                    await preflight(telegram, config)

    async def test_poll_persists_cursor_after_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/state.sqlite3"
            store = Store(path)
            stop = asyncio.Event()
            telegram = AsyncMock()
            moderator = AsyncMock()
            telegram.call.return_value = [{"update_id": 42}, {"update_id": 43}]

            async def handle(update):
                stop.set()

            moderator.handle.side_effect = handle
            await poll(telegram, moderator, store, stop)
            self.assertEqual(store.offset(), 43)
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.offset(), 43)
            reopened.close()

    async def test_unexpected_failure_not_acknowledged(self):
        store = Store(":memory:")
        telegram = AsyncMock()
        telegram.call.return_value = [{"update_id": 42}]
        moderator = AsyncMock()
        moderator.handle.side_effect = RuntimeError("unexpected")
        with self.assertRaises(RuntimeError):
            await poll(telegram, moderator, store, asyncio.Event())
        self.assertIsNone(store.offset())
        store.close()

    async def test_competing_pollers_stop_worker(self):
        telegram = AsyncMock()
        telegram.call.side_effect = APIError("telegram", 409)
        store = Store(":memory:")
        with self.assertRaises(APIError):
            await poll(telegram, AsyncMock(), store, asyncio.Event())
        store.close()
