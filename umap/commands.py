from __future__ import annotations

import logging
from typing import Any

from aiogram import Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from umap.errors import capture_exception, is_transient_network_error


logger = logging.getLogger(__name__)


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
                logger.warning("Manual check failed because of a transient network error: %s", error)
                await message.answer("Временная ошибка сети или uMap. Попробуй /check еще раз чуть позже.")
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
                logger.warning("Test notification failed because of a transient network error: %s", error)
                await message.answer("Временная ошибка сети, Telegram или uMap. Попробуй еще раз чуть позже.")
            else:
                capture_exception(error)
                logger.exception("Test notification failed")
                await message.answer("Тестовое уведомление не удалось отправить. Подробности смотри в логах.")

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
