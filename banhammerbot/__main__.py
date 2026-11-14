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
from .storage import Store

log = logging.getLogger("banhammerbot")


async def preflight(telegram: Telegram, config: Config) -> str:
    me = await telegram.call("getMe")
    webhook = await telegram.call("getWebhookInfo")
    if webhook.get("url"):
        raise ValueError("An existing Telegram webhook is set; remove it before using long polling")
    for chat_id in sorted(config.chat_ids):
        chat = await telegram.call("getChat", chat_id=chat_id)
        if chat.get("type") not in {"group", "supergroup"}:
            raise ValueError(f"Chat {chat_id} is not a group or supergroup")
        member = await telegram.call("getChatMember", chat_id=chat_id, user_id=me["id"])
        if (
            member.get("status") != "administrator"
            or not member.get("can_restrict_members")
            or not member.get("can_delete_messages")
        ):
            raise ValueError(f"Bot needs Ban users and Delete messages admin rights in {chat_id}")
    return me["username"]


async def poll(telegram: Telegram, moderator: Moderator, store: Store, stop: asyncio.Event) -> None:
    backoff = 1
    while not stop.is_set():
        params = {"timeout": 30, "allowed_updates": ["message", "edited_message"], "limit": 50}
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


async def run(config: Config, check: bool = False) -> None:
    # httpx's default INFO request logs contain Telegram's token in the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        telegram = Telegram(client, config.telegram_token, config.max_image_bytes)
        username = await preflight(telegram, config)
        if check:
            log.info("Telegram configuration and admin permissions verified for @%s", username)
            return
        store = Store(config.database_path)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows uses KeyboardInterrupt instead.
                pass
        moderator = Moderator(config, telegram, Jev(client, config), store)
        moderator.username = username
        log.info("Started @%s in %s mode", username, "dry-run" if config.dry_run else "LIVE")
        try:
            await poll(telegram, moderator, store, stop)
        finally:
            store.close()


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
