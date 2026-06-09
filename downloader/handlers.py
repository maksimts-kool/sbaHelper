"""
Обработчики сообщений Telegram-бота загрузчика.
Детектируют ссылки TikTok / YouTube / Facebook и скачивают видео с отчётом прогресса.
"""
import asyncio
import logging

from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from downloader.core import (
    DownloadError,
    DownloadResult,
    FileTooLargeError,
    UnsupportedContentError,
    VideoTooLongError,
    cleanup,
    download_video,
    fetch_info,
)
from downloader.formatting import (
    INFO_EMOJI,
    SEND_VIDEO_EMOJI,
    build_download_progress_line,
    build_status_text,
    build_video_caption,
    escape_md_v2,
    format_duration,
)
from downloader.progress import ProgressStage, rounded_progress_percent
from downloader.service import (
    ALLOWED_CHAT_IDS,
    capture_exception,
    extract_supported_url,
    is_non_video_url,
)

logger = logging.getLogger(__name__)

# Через сколько секунд удалять сообщение с ошибкой проверки ссылки.
REJECTION_DELETE_DELAY_SEC = 5.0


async def _safe_edit(msg: Message, text: str) -> None:
    """Редактирует сообщение, молча игнорируя ошибку 'message not modified'."""
    try:
        await msg.edit_text(text, parse_mode="MarkdownV2")
    except TelegramError as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("edit_text error: %s", e)


async def _safe_delete_message(
    message: Message,
    *,
    bot=None,
    retries: int = 8,
    delay: float = 0.75,
) -> bool:
    """Удаляет сообщение с несколькими попытками на случай временной ошибки Telegram."""
    chat_id = message.chat_id
    message_id = message.message_id

    for attempt in range(1, retries + 1):
        try:
            if bot is not None:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            else:
                await message.delete()
            logger.info("Deleted status message %s in chat %s", message_id, chat_id)
            return True
        except TelegramError as e:
            error_text = str(e).lower()
            if "message to delete not found" in error_text:
                return True

            if attempt == retries:
                logger.warning("delete_message error after %d attempts: %s", retries, e)
                return False

            retry_after = getattr(e, "retry_after", None)
            await asyncio.sleep(float(retry_after) if retry_after else delay)

    return False


async def _show_then_delete(
    status_msg: Message,
    text: str,
    *,
    bot,
    delay: float = REJECTION_DELETE_DELAY_SEC,
) -> None:
    """Показывает текст ошибки и удаляет сообщение через `delay` секунд."""
    await _safe_edit(status_msg, text)
    await asyncio.sleep(delay)
    await _safe_delete_message(status_msg, bot=bot)


def _reject_with_transient_error(
    context: ContextTypes.DEFAULT_TYPE,
    status_msg: Message,
    text: str,
) -> None:
    """Ссылка не прошла проверку: показываем ошибку и удаляем её через 5 секунд.

    Удаление выполняется фоновой задачей, чтобы не блокировать обработку
    других сообщений на время паузы.
    """
    context.application.create_task(
        _show_then_delete(status_msg, text, bot=context.bot)
    )


# --------------------------------------------------------------------------- #
#  Основной обработчик                                                         #
# --------------------------------------------------------------------------- #

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Реагирует на любое текстовое сообщение / подпись к медиа.
    Если обнаружена поддерживаемая ссылка — скачивает видео.
    """
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    chat = update.effective_chat
    user_label = f"{user.full_name} (id={user.id})" if user else "unknown"
    chat_label = f"{chat.title or chat.type} (id={chat.id})" if chat else "unknown"
    chat_id = chat.id

    # Разрешаем только заданные чаты
    if ALLOWED_CHAT_IDS and chat.id not in ALLOWED_CHAT_IDS:
        logger.debug("Ignored message from disallowed chat %s (user: %s)", chat_label, user_label)
        return

    text = message.text or message.caption or ""
    url = extract_supported_url(text)
    if not url:
        logger.debug("No supported URL in message from %s in %s", user_label, chat_label)
        return

    # Профиль / канал / подборка — это не отдельное видео, молча игнорируем,
    # чтобы не пытаться скачать чужой профиль целиком.
    if is_non_video_url(url):
        logger.info("[%s] Ignoring non-video URL (profile/listing): %s", chat_label, url)
        return

    logger.info("[%s] %s requested URL: %s", chat_label, user_label, url)

    # --- 1. Проверяем ссылку: это короткое вертикальное видео (shorts)? ---
    status_msg = await message.reply_text(
        f"{INFO_EMOJI} Проверяю ссылку\\.\\.\\.",
        parse_mode="MarkdownV2",
    )

    loop = asyncio.get_running_loop()

    try:
        info = await loop.run_in_executor(
            None, fetch_info, url
        )
    except UnsupportedContentError as e:
        # Фото-пост, трансляция, горизонтальное видео и т.п. — мягко поясняем.
        _reject_with_transient_error(context, status_msg, f"{INFO_EMOJI} {escape_md_v2(str(e))}")
        return
    except VideoTooLongError as e:
        _reject_with_transient_error(context, status_msg, f"⏱ {escape_md_v2(str(e))}")
        return
    except DownloadError as e:
        short_err = str(e)[:200]
        await _safe_edit(
            status_msg,
            f"❌ Не удалось получить информацию:\n`{escape_md_v2(short_err)}`",
        )
        return
    except Exception as e:
        capture_exception(e)
        logger.exception("Unexpected error in fetch_info")
        await _safe_edit(status_msg, "❌ Неизвестная ошибка при получении информации\\.")
        return

    # --- 2. Ссылка прошла проверку — получаем информацию о видео ---
    await _safe_edit(status_msg, f"{INFO_EMOJI} Получаю информацию о видео\\.\\.\\.")

    duration_str = f" · {format_duration(info.duration)}" if info.duration else ""
    await _safe_edit(
        status_msg,
        build_status_text(
            info.title,
            info.uploader,
            duration_str,
            build_download_progress_line(0),
        ),
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    # --- 3. Скачиваем ---
    result: DownloadResult | None = None
    last_reported_pct = 0
    progress_stage = ProgressStage()
    logger.info("[%s] Starting download: %s", chat_label, url)

    async def _apply_download_status(progress_pct: int, expected_version: int) -> None:
        if not progress_stage.is_current("download", expected_version):
            return

        await _safe_edit(
            status_msg,
            build_status_text(
                info.title,
                info.uploader,
                duration_str,
                build_download_progress_line(progress_pct),
            ),
        )

    def on_progress(pct: float) -> None:
        nonlocal last_reported_pct
        # Показываем прогресс с 0% и не даём ему перескакивать назад.
        rounded = rounded_progress_percent(pct)

        stage, expected_version = progress_stage.snapshot()
        if stage != "download":
            return

        if rounded > last_reported_pct:
            last_reported_pct = rounded
            logger.info("[%s] Download progress: %d%%", chat_label, rounded)
            asyncio.run_coroutine_threadsafe(
                _apply_download_status(rounded, expected_version),
                loop,
            )

    try:
        result = await loop.run_in_executor(
            None, lambda: download_video(url, on_progress=on_progress)
        )
    except VideoTooLongError as e:
        logger.warning("[%s] Video too long at download stage: %s", chat_label, url)
        await _safe_edit(status_msg, f"⏱ {escape_md_v2(str(e))}")
        return
    except FileTooLargeError as e:
        logger.warning("[%s] File too large: %s", chat_label, url)
        await _safe_edit(status_msg, f"📦 {escape_md_v2(str(e))}")
        return
    except DownloadError as e:
        logger.error("[%s] Download failed for %s: %s", chat_label, url, e)
        short_err = str(e)[:200]
        await _safe_edit(status_msg, f"❌ Ошибка загрузки:\n`{escape_md_v2(short_err)}`")
        return
    except Exception as e:
        logger.exception("[%s] Unexpected error in download_video for %s", chat_label, url)
        capture_exception(e)
        await _safe_edit(status_msg, "❌ Неизвестная ошибка при скачивании\\.")
        return

    logger.info("[%s] Download complete: %s", chat_label, result.file_path)
    await _safe_edit(
        status_msg,
        build_status_text(
            info.title,
            info.uploader,
            duration_str,
            build_download_progress_line(100),
        ),
    )

    # --- 4. Отправляем ---
    logger.info("[%s] Sending video to chat...", chat_label)
    progress_stage.advance("sending")
    await _safe_edit(
        status_msg,
        build_status_text(
            info.title,
            info.uploader,
            duration_str,
            f"{SEND_VIDEO_EMOJI} Отправляю видео\\.\\.\\.",
        ),
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    sent_successfully = False
    try:
        with open(result.file_path, "rb") as video_file:
            caption = build_video_caption(result.info)
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=caption,
                parse_mode="HTML",
                supports_streaming=True,
                reply_to_message_id=message.message_id,
            )
    except TelegramError as e:
        logger.error("[%s] Failed to send video: %s", chat_label, e)
        await _safe_edit(status_msg, f"❌ Не удалось отправить видео: {escape_md_v2(str(e))}")
    else:
        sent_successfully = True
        logger.info("[%s] Video sent successfully to %s", chat_label, user_label)
        await _safe_delete_message(status_msg, bot=context.bot)
    finally:
        cleanup(result.file_path)

    if not sent_successfully:
        return
