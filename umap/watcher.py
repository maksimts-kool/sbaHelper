from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from sbahelper.logging import configure_logging as configure_shared_logging
from umap.checks import print_results, result_exit_code, run_checks
from umap.client import UmapClient
from umap.commands import build_dispatcher
from umap.errors import flush_error_tracking, init_error_tracking
from umap.loops import watch_change_loop, watch_loop
from umap.route_service import RouteWatcherService
from umap.settings import env, env_bool, load_bot_settings
from umap.state import build_state_store


logger = logging.getLogger(__name__)


def configure_logging(log_level: str) -> None:
    configure_shared_logging(log_level, quiet_loggers=("httpx", "httpcore"))


async def run_bot() -> None:
    settings = load_bot_settings()
    configure_logging(settings.log_level)
    init_error_tracking("umap-route-bot")

    state_store = build_state_store(settings)
    state = await asyncio.to_thread(state_store.load)
    if (
        settings.default_subscriber_chat_id is not None
        and settings.default_subscriber_chat_id not in state.subscriber_chat_ids
    ):
        state.subscriber_chat_ids.add(settings.default_subscriber_chat_id)
        await asyncio.to_thread(state_store.save, state)

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    umap_clients = {
        layer.key: UmapClient(
            datalayer_url=settings.build_datalayer_url(layer),
            timeout_seconds=settings.request_timeout_seconds,
            retry_attempts=settings.request_retry_attempts,
            retry_backoff_seconds=settings.request_retry_backoff_seconds,
        )
        for layer in settings.watched_layers
    }
    service = RouteWatcherService(
        settings=settings,
        state_store=state_store,
        state=state,
        umap_clients=umap_clients,
        bot=bot,
    )
    dp = build_dispatcher(service)

    watcher_task = asyncio.create_task(
        watch_loop(service, settings.poll_interval_seconds),
        name="umap-watch-loop",
    )
    change_watcher_task = asyncio.create_task(
        watch_change_loop(service, settings.change_poll_interval_seconds),
        name="umap-change-watch-loop",
    )

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        watcher_task.cancel()
        change_watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task
        with suppress(asyncio.CancelledError):
            await change_watcher_task
        for umap_client in umap_clients.values():
            await umap_client.close()
        await bot.session.close()


def run_startup_then_bot() -> int:
    configure_logging(env("LOG_LEVEL", "INFO").upper())
    init_error_tracking("umap-startup")

    logger.info("Running startup uMap checks.")
    results = asyncio.run(run_checks())
    print_results(results)
    exit_code = result_exit_code(results)
    flush_error_tracking()

    if exit_code:
        if env_bool("UMAP_STARTUP_CHECKS_REQUIRED", True):
            logger.error("Startup uMap checks failed with exit code %s.", exit_code)
            return exit_code
        logger.warning(
            "Startup uMap checks failed with exit code %s. Continuing because "
            "UMAP_STARTUP_CHECKS_REQUIRED=0.",
            exit_code,
        )

    asyncio.run(run_bot())
    return 0


def main() -> int:
    return run_startup_then_bot()


if __name__ == "__main__":
    raise SystemExit(main())
