"""
uMap route bot: container entrypoint plus the startup checks, polling loops,
and Telegram command handlers.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from shared import (
    configure_logging as configure_shared_logging,
)
from shared import (
    print_results as print_startup_results,
)
from umap.config import (
    BotSettings,
    WatchedLayer,
    capture_exception,
    env,
    env_bool,
    flush_error_tracking,
    init_error_tracking,
    is_transient_network_error,
    load_bot_settings,
)
from umap.service import RouteWatcherService, UmapClient, build_state_store

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Startup checks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UmapCheckResult:
    name: str
    ok: bool
    message: str
    blocks_startup: bool = True


async def _check_layer(settings: BotSettings, layer: WatchedLayer) -> UmapCheckResult:
    client = UmapClient(
        datalayer_url=settings.build_datalayer_url(layer),
        timeout_seconds=settings.request_timeout_seconds,
        retry_attempts=settings.request_retry_attempts,
        retry_backoff_seconds=settings.request_retry_backoff_seconds,
    )
    try:
        routes = await client.fetch_routes()
    except Exception as error:
        capture_exception(error)
        logger.exception("uMap check failed for layer %s", layer.title)
        return UmapCheckResult(layer.key, False, str(error))
    finally:
        await client.close()

    return UmapCheckResult(
        layer.key,
        True,
        f"{layer.title}: fetched {len(routes)} routes",
    )


async def run_checks() -> list[UmapCheckResult]:
    try:
        settings = load_bot_settings()
    except RuntimeError as error:
        capture_exception(error)
        return [UmapCheckResult("settings", False, str(error))]

    results = [
        UmapCheckResult(
            "settings",
            True,
            f"configured {len(settings.watched_layers)} layer(s)",
        )
    ]

    try:
        await asyncio.to_thread(build_state_store(settings).load)
    except Exception as error:
        capture_exception(error)
        logger.exception("SQLite state store check failed")
        results.append(UmapCheckResult("state-store", False, str(error)))
    else:
        results.append(
            UmapCheckResult(
                "state-store",
                True,
                f"SQLite {settings.state_db_path}",
            )
        )

    for layer in settings.watched_layers:
        results.append(await _check_layer(settings, layer))
    return results


def print_results(results: list[UmapCheckResult]) -> None:
    print_startup_results(results)


def result_exit_code(results: list[UmapCheckResult]) -> int:
    if any(result.name == "settings" and not result.ok for result in results):
        return 2
    if any(not result.ok for result in results):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Polling loops
# --------------------------------------------------------------------------- #


async def watch_loop(service: Any, interval_seconds: int) -> None:
    while True:
        try:
            results = await service.check_for_updates(notify=True)
            for layer in service.watched_layers:
                result = results.get(layer.key)
                if result is None:
                    continue
                logger.info(
                    "Layer %s checked: current=%s known_before=%s known_after=%s new=%s",
                    layer.title,
                    result.current_feature_count,
                    result.previous_known_feature_count,
                    result.known_feature_count,
                    len(result.new_features),
                )
        except Exception as error:
            if is_transient_network_error(error):
                logger.warning(
                    "Scheduled layer check skipped after transient network failure: %s", error
                )
            else:
                capture_exception(error)
                logger.exception("Scheduled layer check failed")

        await asyncio.sleep(interval_seconds)


async def watch_change_loop(service: Any, interval_seconds: int) -> None:
    while True:
        try:
            changes_by_layer = await service.check_for_route_changes(notify=True)
            for layer in service.watched_layers:
                layer_changes = changes_by_layer.get(layer.key, [])
                logger.info(
                    "Layer %s route changes checked: changed=%s", layer.title, len(layer_changes)
                )
        except Exception as error:
            if is_transient_network_error(error):
                logger.warning(
                    "Scheduled route change check skipped after transient network failure: %s",
                    error,
                )
            else:
                capture_exception(error)
                logger.exception("Scheduled route change check failed")

        await asyncio.sleep(interval_seconds)


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #


def build_dispatcher(service: Any) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def handle_start(message: Message) -> None:
        await message.answer(service.start_message())

    @dp.message(Command("subscribe"))
    async def handle_subscribe(message: Message) -> None:
        added = await service.subscribe(message.chat.id)
        text = "Чат подписан на уведомления." if added else "Этот чат уже подписан."
        await message.answer(text)

    @dp.message(Command("unsubscribe"))
    async def handle_unsubscribe(message: Message) -> None:
        removed = await service.unsubscribe(message.chat.id)
        text = "Чат отписан от уведомлений." if removed else "Этот чат не был подписан."
        await message.answer(text)

    @dp.message(Command("status"))
    async def handle_status(message: Message) -> None:
        await message.answer(service.status_message())

    @dp.message(Command("chatid"))
    async def handle_chat_id(message: Message) -> None:
        await message.answer(f"chat_id: <code>{message.chat.id}</code>")

    @dp.message(Command("check"))
    async def handle_check(message: Message) -> None:
        try:
            results = await service.check_for_updates(notify=True)
        except Exception as error:
            if is_transient_network_error(error):
                logger.warning(
                    "Manual check failed because of a transient network error: %s", error
                )
                await message.answer(
                    "Временная ошибка сети или uMap. Попробуй /check еще раз чуть позже."
                )
            else:
                capture_exception(error)
                logger.exception("Manual check failed")
                await message.answer("Не удалось выполнить проверку. Подробности смотри в логах.")
            return

        total_new_routes = sum(len(result.new_features) for result in results.values())
        if total_new_routes == 0:
            await message.answer("Проверка завершена. Новых маршрутов не найдено ни в одном слое.")
            return

        lines = ["Проверка завершена."]
        for layer in service.watched_layers:
            result = results.get(layer.key)
            if result is not None:
                lines.append(f"{layer.title}: новых маршрутов {len(result.new_features)}.")
        await message.answer("\n".join(lines))

    @dp.message(Command("testnotify"))
    async def handle_test_notify(message: Message) -> None:
        try:
            await service.send_test_notification(message.chat.id)
        except Exception as error:
            if is_transient_network_error(error):
                logger.warning(
                    "Test notification failed because of a transient network error: %s", error
                )
                await message.answer(
                    "Временная ошибка сети, Telegram или uMap. Попробуй еще раз чуть позже."
                )
            else:
                capture_exception(error)
                logger.exception("Test notification failed")
                await message.answer(
                    "Тестовое уведомление не удалось отправить. Подробности смотри в логах."
                )

    @dp.message(F.text, F.chat.type == ChatType.PRIVATE)
    async def handle_fallback(message: Message) -> None:
        await message.answer("Используй /start, чтобы увидеть доступные команды.")

    @dp.message()
    async def handle_ignored_message(message: Message) -> None:
        logger.debug(
            "Ignored non-command message in chat %s (%s).",
            message.chat.id,
            message.chat.type,
        )

    return dp


# --------------------------------------------------------------------------- #
# Runtime + entrypoint
# --------------------------------------------------------------------------- #


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
