import asyncio
import os
import signal
import sys
import tempfile
import textwrap
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

    def test_missing_or_empty_group_list_enables_all_groups_mode(self):
        env = {"TELEGRAM_BOT_TOKEN": "token", "TYPESAFE_API_KEY": "key"}
        for settings in (env, {**env, "TELEGRAM_CHAT_IDS": ""}, {**env, "TELEGRAM_CHAT_IDS": "  "}):
            with self.subTest(settings=settings), patch.dict(os.environ, settings, clear=True):
                self.assertEqual(Config.from_env().chat_ids, frozenset())
        self.assertEqual(Config("token", "key").chat_ids, frozenset())


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_chat_failure_points_to_configuration(self):
        telegram = AsyncMock()
        telegram.call.side_effect = [
            {"id": 12, "username": "bot"},
            {"url": ""},
            APIError(
                "telegram",
                400,
                method="getChat",
                chat_id=-1001234567890,
                description="Bad Request: chat not found",
            ),
        ]
        with self.assertRaises(ValueError) as caught:
            await preflight(telegram, Config("token", "key", frozenset({-1001234567890})))
        self.assertIn("telegram.getChat", str(caught.exception))
        self.assertIn("chat not found", str(caught.exception))
        self.assertIn("TELEGRAM_CHAT_IDS", str(caught.exception))
        self.assertIn("empty to moderate all groups", str(caught.exception))

    async def test_preflight_all_groups_mode_does_not_require_known_groups(self):
        telegram = AsyncMock()
        telegram.call.side_effect = [{"id": 12, "username": "bot"}, {"url": ""}]
        self.assertEqual(await preflight(telegram, Config("token", "key")), (12, "bot"))
        self.assertEqual(
            [c.args[0] for c in telegram.call.call_args_list], ["getMe", "getWebhookInfo"]
        )

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
                self.assertEqual(await preflight(telegram, config), (12, "bot"))
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


@unittest.skipUnless(os.name == "posix", "POSIX signals are required")
class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def assert_prompt_shutdown(self, stage: str, sig: signal.Signals):
        # A separate process exercises real signal delivery without signalling the test runner.
        # The API is replaced with an indefinitely waiting coroutine; no network/credentials used.
        script = textwrap.dedent("""
            import asyncio
            import sys
            from unittest.mock import patch
            from banhammerbot.__main__ import main

            stage = sys.argv[1]

            async def block():
                print("READY", flush=True)
                try:
                    await asyncio.Future()
                finally:
                    print("CANCELLED", flush=True)

            async def call(self, method, **params):
                if method == stage:
                    await block()
                return {
                    "getMe": {"id": 12, "username": "test_bot"},
                    "getWebhookInfo": {"url": ""},
                    "getUpdates": [{"update_id": 42}],
                    "setMyCommands": True,
                    "setChatMenuButton": True,
                }[method]

            async def handle(self, update):
                await block()

            sys.argv = [sys.argv[0]]
            with patch("banhammerbot.__main__.Telegram.call", call), \\
                 patch("banhammerbot.__main__.Moderator.handle", handle):
                main()
        """)
        with tempfile.TemporaryDirectory() as directory:
            database = directory + "/state.sqlite3"
            env = {
                **os.environ,
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TYPESAFE_API_KEY": "test-key",
                "TELEGRAM_CHAT_IDS": "",
                "DATABASE_PATH": database,
                "DRY_RUN": "true",
            }
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-c",
                script,
                stage,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                ready = await asyncio.wait_for(process.stdout.readline(), timeout=5)
                self.assertEqual(ready, b"READY\n")
                process.send_signal(sig)
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertIn(b"CANCELLED", stdout)
                self.assertIn(b"Bot stopped", stderr)
                self.assertNotIn(b"Traceback", stderr)
                if stage == "moderation":
                    store = Store(database)
                    try:
                        self.assertIsNone(store.offset())
                    finally:
                        store.close()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()

    async def test_ctrl_c_during_startup(self):
        await self.assert_prompt_shutdown("getMe", signal.SIGINT)

    async def test_ctrl_c_cancels_long_poll(self):
        await self.assert_prompt_shutdown("getUpdates", signal.SIGINT)

    async def test_sigterm_cancels_long_poll(self):
        await self.assert_prompt_shutdown("getUpdates", signal.SIGTERM)

    async def test_ctrl_c_cancels_moderation_without_acknowledging(self):
        await self.assert_prompt_shutdown("moderation", signal.SIGINT)
