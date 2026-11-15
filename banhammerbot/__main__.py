import argparse
import asyncio
import logging
import signal

import httpx
from dotenv import load_dotenv

from .api import APIError, Telegram
from .config import Config
from .decision import Jev
from .moderation import Moderator
from .permissions import has_moderation_rights
from .storage import Store

log = logging.getLogger("banhammerbot")


async def preflight(telegram: Telegram, config: Config) -> tuple[int, str]:
    me = await telegram.call("getMe")
    webhook = await telegram.call("getWebhookInfo")
    if webhook.get("url"):
        raise ValueError("An existing Telegram webhook is set; remove it before using long polling")
    for chat_id in sorted(config.chat_ids):
        try:
            chat = await telegram.call("getChat", chat_id=chat_id)
            member = await telegram.call("getChatMember", chat_id=chat_id, user_id=me["id"])
        except APIError as exc:
            raise ValueError(
                f"{exc}. Check TELEGRAM_CHAT_IDS in .env and add the bot to that group "
                "as an administrator. Leave TELEGRAM_CHAT_IDS empty to moderate all groups "
                "where the bot has Ban users and Delete messages rights."
            ) from None
        if chat.get("type") not in {"group", "supergroup"}:
            raise ValueError(f"Chat {chat_id} is not a group or supergroup")
        if not has_moderation_rights(member):
            raise ValueError(f"Bot needs Ban users and Delete messages admin rights in {chat_id}")
    return me["id"], me["username"]


async def poll(telegram: Telegram, moderator: Moderator, store: Store, stop: asyncio.Event) -> None:
    backoff = 1
    while not stop.is_set():
        params = {
            "timeout": 30,
            "limit": 50,
            "allowed_updates": [
                "message",
                "edited_message",
                "callback_query",
                "my_chat_member",
                "chat_member",
            ],
        }
        if store.offset() is not None:
            params["offset"] = store.offset()
        try:
            updates = await telegram.call("getUpdates", **params)
        except APIError as exc:
            if exc.code in {401, 403, 409}:
                # Wrong token or a competing poller/webhook requires operator intervention.
                raise
            log.warning("Polling failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=exc.retry_after or backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, 30)
            continue
        backoff = 1
        for update in updates:
            if stop.is_set():
                break
            await moderator.handle(update)
            # Unexpected exceptions stop the worker before acknowledging; they are never discarded.
            store.acknowledge(update["update_id"])


async def serve(config: Config, check: bool, stop: asyncio.Event) -> None:
    # httpx's default INFO request logs contain Telegram's token in the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        telegram = Telegram(client, config.telegram_token, config.max_image_bytes)
        bot_id, username = await preflight(telegram, config)
        if check:
            if config.chat_ids:
                log.info(
                    "Telegram configuration and listed-group permissions verified for @%s", username
                )
            else:
                log.info(
                    "Telegram authentication verified for @%s; all-groups mode enabled. "
                    "Group permissions will be checked on each message.",
                    username,
                )
            return
        store = Store(config.database_path)
        moderator = Moderator(config, telegram, Jev(client, config), store, bot_id=bot_id)
        moderator.username = username
        log.info(
            "Started @%s; default mode=%s (group overrides apply); scope=%s",
            username,
            "dry-run" if config.dry_run else "LIVE",
            "listed groups" if config.chat_ids else "all groups with moderation permissions",
        )
        try:
            await moderator.management.register()
            await poll(telegram, moderator, store, stop)
        finally:
            store.close()


async def run(config: Config, check: bool = False) -> None:
    stop = asyncio.Event()
    worker = asyncio.create_task(serve(config, check, stop))
    loop = asyncio.get_running_loop()
    previous_handlers = {}

    def request_shutdown() -> None:
        if not stop.is_set():
            log.info("Stopping bot; cancelling active requests")
            stop.set()
            worker.cancel()

    try:
        # Install before startup requests too. Cancellation unwinds the HTTP client and SQLite
        # finally blocks; an interrupted update is not acknowledged and can be retried on restart.
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(sig)
            try:
                loop.add_signal_handler(sig, request_shutdown)
            except NotImplementedError:  # asyncio.run handles Ctrl+C on Windows.
                continue
            previous_handlers[sig] = previous
        try:
            await worker
        except asyncio.CancelledError:
            if not stop.is_set():
                raise
        if stop.is_set():
            log.info("Bot stopped")
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        for sig, previous in previous_handlers.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, previous)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram spam moderation with Jev multimodal")
    parser.add_argument(
        "--check", action="store_true", help="Verify Telegram access without polling"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    try:
        asyncio.run(run(Config.from_env(), args.check))
    except (ValueError, APIError) as exc:
        log.error("%s", exc)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Avoid traceback locals and exception URLs that might expose credentials or user content.
        log.error("Worker stopped after unexpected %s; cursor retained", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
