"""
Обработчики сообщений Telegram-бота загрузчика.
Детектируют ссылки TikTok / YouTube Shorts и скачивают видео с отчётом прогресса.
"""
import asyncio
import logging
import os
import re

from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from downloader.config import ALLOWED_CHAT_IDS, SUPPORTED_URL_PATTERNS
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

logger = logging.getLogger(__name__)

INFO_EMOJI = "![ℹ️](tg://emoji?id=5231012545799666522)"
DOWNLOAD_EMOJI = "![⬇️](tg://emoji?id=5386367538735104399)"
SEND_VIDEO_EMOJI = "![📤](tg://emoji?id=5201691993775818138)"


# --------------------------------------------------------------------------- #
#  Вспомогательные функции                                                     #
# --------------------------------------------------------------------------- #

def _extract_url(text: str) -> str | None:
    """Возвращает первую поддерживаемую ссылку из текста, или None."""
    for pattern in SUPPORTED_URL_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0).rstrip(".,);")  # убираем лишние символы конца
    return None


def _format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _escape_md_v2(text: str) -> str:
    return re.sub(r"([_\*\[\]\(\)~`>#+\-=|{}.!])", r"\\\1", text)


def _build_status_text(title: str, uploader: str, duration_str: str, status_line: str) -> str:
    return (
        f"📹 *{_escape_md_v2(title)}*\n"
        f"👤 {_escape_md_v2(uploader)}{_escape_md_v2(duration_str)}\n\n"
        f"{status_line}"
    )


async def _safe_edit(msg: Message, text: str) -> None:
    """Редактирует сообщение, молча игнорируя ошибку 'message not modified'."""
    try:
        await msg.edit_text(text, parse_mode="MarkdownV2")
    except TelegramError as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("edit_text error: %s", e)


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
    url = _extract_url(text)
    if not url:
        logger.debug("No supported URL in message from %s in %s", user_label, chat_label)
        return

    logger.info("[%s] %s requested URL: %s", chat_label, user_label, url)

    # --- 1. Получаем информацию ---
    status_msg = await message.reply_text(
        f"{INFO_EMOJI} Получаю информацию о видео\.\.\.",
        parse_mode="MarkdownV2",
    )

    loop = asyncio.get_running_loop()

    try:
        info = await loop.run_in_executor(
            None, fetch_info, url
        )
    except UnsupportedContentError as e:
        await _safe_edit(status_msg, f"🚫 {_escape_md_v2(str(e))}")
        return
    except VideoTooLongError as e:
        await _safe_edit(status_msg, f"⏱ {_escape_md_v2(str(e))}")
        return
    except DownloadError as e:
        short_err = str(e)[:200]
        await _safe_edit(
            status_msg,
            f"❌ Не удалось получить информацию:\n`{_escape_md_v2(short_err)}`",
        )
        return
    except Exception as e:
        logger.exception("Unexpected error in fetch_info")
        await _safe_edit(status_msg, "❌ Неизвестная ошибка при получении информации\.")
        return

    duration_str = f" · {_format_duration(info.duration)}" if info.duration else ""
    await _safe_edit(
        status_msg,
        _build_status_text(
            info.title,
            info.uploader,
            duration_str,
            f"{DOWNLOAD_EMOJI} Скачиваю видео\.\.\.",
        ),
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    # --- 2. Скачиваем ---
    result: DownloadResult | None = None
    last_reported_pct = -1
    logger.info("[%s] Starting download: %s", chat_label, url)

    def on_progress(pct: float) -> None:
        nonlocal last_reported_pct
        # обновляем статус только каждые 20%
        rounded = int(pct // 20) * 20
        if rounded != last_reported_pct:
            last_reported_pct = rounded
            bar = "▓" * (rounded // 10) + "░" * (10 - rounded // 10)
            logger.info("[%s] Download progress: %d%%", chat_label, rounded)
            # Безопасный вызов корутины из потока без event loop
            asyncio.run_coroutine_threadsafe(
                _safe_edit(
                    status_msg,
                    _build_status_text(
                        info.title,
                        info.uploader,
                        duration_str,
                        f"{DOWNLOAD_EMOJI} Скачиваю: \[{bar}\] {rounded}%",
                    ),
                ),
                loop,
            )

    try:
        result = await loop.run_in_executor(
            None, lambda: download_video(url, on_progress=on_progress)
        )
    except VideoTooLongError as e:
        logger.warning("[%s] Video too long at download stage: %s", chat_label, url)
        await _safe_edit(status_msg, f"⏱ {_escape_md_v2(str(e))}")
        return
    except FileTooLargeError as e:
        logger.warning("[%s] File too large: %s", chat_label, url)
        await _safe_edit(status_msg, f"📦 {_escape_md_v2(str(e))}")
        return
    except DownloadError as e:
        logger.error("[%s] Download failed for %s: %s", chat_label, url, e)
        short_err = str(e)[:200]
        await _safe_edit(status_msg, f"❌ Ошибка загрузки:\n`{_escape_md_v2(short_err)}`")
        return
    except Exception:
        logger.exception("[%s] Unexpected error in download_video for %s", chat_label, url)
        await _safe_edit(status_msg, "❌ Неизвестная ошибка при скачивании\.")
        return

    logger.info("[%s] Download complete: %s", chat_label, result.file_path)

    # --- 3. Отправляем ---
    logger.info("[%s] Sending video to chat...", chat_label)
    await _safe_edit(
        status_msg,
        _build_status_text(
            info.title,
            info.uploader,
            duration_str,
            f"{SEND_VIDEO_EMOJI} Отправляю видео\.\.\.",
        ),
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    try:
        with open(result.file_path, "rb") as video_file:
            caption = (
                f"🎬 <b>{info.title}</b>\n"
                f"👤 {info.uploader}{duration_str}"
            )
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
        await _safe_edit(status_msg, f"❌ Не удалось отправить видео: {_escape_md_v2(str(e))}")
        return
    else:
        logger.info("[%s] Video sent successfully to %s", chat_label, user_label)
    finally:
        cleanup(result.file_path)

    # Удаляем статусное сообщение
    try:
        await status_msg.delete()
    except TelegramError:
        pass
