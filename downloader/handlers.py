"""
Обработчики сообщений Telegram-бота загрузчика.
Детектируют ссылки TikTok / YouTube / Facebook и скачивают видео с отчётом прогресса.
"""
import asyncio
import html
import logging
import os
import re
import threading

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
from downloader.service import ALLOWED_CHAT_IDS, capture_exception, extract_supported_url

logger = logging.getLogger(__name__)

INFO_EMOJI = "![ℹ️](tg://emoji?id=5231012545799666522)"
DOWNLOAD_EMOJI = "![⬇️](tg://emoji?id=5386367538735104399)"
SEND_VIDEO_EMOJI = "![📤](tg://emoji?id=5201691993775818138)"
LENGTH_EMOJI_ID = "5350438526691326210"
VIEWS_EMOJI_ID = "5210956306952758910"
LIKES_EMOJI_ID = "5337080053119336309"
VIDEO_CAPTION_AD = "Большие видео или аудио можно скачать тут https://sba-ytdlp.vercel.app/"


# --------------------------------------------------------------------------- #
#  Вспомогательные функции                                                     #
# --------------------------------------------------------------------------- #

def _format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def _format_count(value: int | None) -> str:
    if value is None:
        return "—"
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        compact = value / 1_000
        suffix = "K"
    elif value < 1_000_000_000:
        compact = value / 1_000_000
        suffix = "M"
    else:
        compact = value / 1_000_000_000
        suffix = "B"

    formatted = f"{compact:.1f}".rstrip("0").rstrip(".")
    return f"{formatted}{suffix}"


def _prevent_auto_links(text: str) -> str:
    """Ломает авто-детект Telegram для hashtag / mention / url / timestamps."""
    sanitized = text.replace("#", "#\u2060").replace("@", "@\u2060")
    sanitized = re.sub(r"(?i)\bhttps?://", lambda m: m.group(0)[0] + "\u2060" + m.group(0)[1:], sanitized)
    sanitized = re.sub(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", lambda m: m.group(0).replace(":", ":\u2060"), sanitized)
    return sanitized


def _build_video_caption(info) -> str:
    duration_text = _prevent_auto_links(_format_duration(info.duration) if info.duration else "0:00")
    title = html.escape(_prevent_auto_links(info.title))
    uploader = html.escape(_prevent_auto_links(info.uploader))
    lines = [
        f"🎬 <b>{title}</b> | <tg-emoji emoji-id=\"{LENGTH_EMOJI_ID}\">⏱️</tg-emoji> {duration_text}"
    ]

    if info.view_count is not None:
        lines.append(
            f"<tg-emoji emoji-id=\"{VIEWS_EMOJI_ID}\">👁️</tg-emoji> {_format_count(info.view_count)} просмотров"
        )

    if info.like_count is not None:
        lines.append(
            f"<tg-emoji emoji-id=\"{LIKES_EMOJI_ID}\">❤️</tg-emoji> {_format_count(info.like_count)} лайков"
        )

    lines.append(f"👤 {uploader}")
    lines.extend(["", html.escape(VIDEO_CAPTION_AD)])
    return "\n".join(lines)


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


def _build_download_progress_line(progress_pct: int) -> str:
    bounded_pct = max(0, min(progress_pct, 100))
    bar = "▓" * (bounded_pct // 10) + "░" * (10 - bounded_pct // 10)
    return f"{DOWNLOAD_EMOJI} Скачиваю: \\[{bar}\\] {bounded_pct}%"


async def _safe_delete_message(message: Message, retries: int = 3, delay: float = 0.4) -> None:
    """Удаляет сообщение с несколькими попытками на случай временной ошибки Telegram."""
    for attempt in range(1, retries + 1):
        try:
            await message.delete()
            return
        except TelegramError as e:
            error_text = str(e).lower()
            if "message to delete not found" in error_text:
                return

            if attempt == retries:
                logger.warning("delete_message error after %d attempts: %s", retries, e)
                return

            await asyncio.sleep(delay)


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

    logger.info("[%s] %s requested URL: %s", chat_label, user_label, url)

    # --- 1. Получаем информацию ---
    status_msg = await message.reply_text(
        f"{INFO_EMOJI} Получаю информацию о видео\\.\\.\\.",
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
        capture_exception(e)
        logger.exception("Unexpected error in fetch_info")
        await _safe_edit(status_msg, "❌ Неизвестная ошибка при получении информации\\.")
        return

    duration_str = f" · {_format_duration(info.duration)}" if info.duration else ""
    await _safe_edit(
        status_msg,
        _build_status_text(
            info.title,
            info.uploader,
            duration_str,
            _build_download_progress_line(0),
        ),
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    # --- 2. Скачиваем ---
    result: DownloadResult | None = None
    last_reported_pct = 0
    progress_state = {"stage": "download", "version": 0}
    progress_state_lock = threading.Lock()
    logger.info("[%s] Starting download: %s", chat_label, url)

    async def _apply_download_status(progress_pct: int, expected_version: int) -> None:
        with progress_state_lock:
            is_current_download_stage = (
                progress_state["stage"] == "download" and progress_state["version"] == expected_version
            )
        if not is_current_download_stage:
            return

        await _safe_edit(
            status_msg,
            _build_status_text(
                info.title,
                info.uploader,
                duration_str,
                _build_download_progress_line(progress_pct),
            ),
        )

    def on_progress(pct: float) -> None:
        nonlocal last_reported_pct
        # Показываем прогресс с 0% и не даём ему перескакивать назад.
        bounded = min(100, max(0, int(pct)))
        rounded = (bounded // 10) * 10

        with progress_state_lock:
            if progress_state["stage"] != "download":
                return
            expected_version = progress_state["version"]

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
    except Exception as e:
        logger.exception("[%s] Unexpected error in download_video for %s", chat_label, url)
        capture_exception(e)
        await _safe_edit(status_msg, "❌ Неизвестная ошибка при скачивании\\.")
        return

    logger.info("[%s] Download complete: %s", chat_label, result.file_path)
    await _safe_edit(
        status_msg,
        _build_status_text(
            info.title,
            info.uploader,
            duration_str,
            _build_download_progress_line(100),
        ),
    )

    # --- 3. Отправляем ---
    logger.info("[%s] Sending video to chat...", chat_label)
    with progress_state_lock:
        progress_state["stage"] = "sending"
        progress_state["version"] += 1
    await _safe_edit(
        status_msg,
        _build_status_text(
            info.title,
            info.uploader,
            duration_str,
            f"{SEND_VIDEO_EMOJI} Отправляю видео\\.\\.\\.",
        ),
    )
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    send_failed = False
    try:
        with open(result.file_path, "rb") as video_file:
            caption = _build_video_caption(result.info)
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=caption,
                parse_mode="HTML",
                supports_streaming=True,
                reply_to_message_id=message.message_id,
            )
    except TelegramError as e:
        send_failed = True
        logger.error("[%s] Failed to send video: %s", chat_label, e)
        await _safe_edit(status_msg, f"❌ Не удалось отправить видео: {_escape_md_v2(str(e))}")
    else:
        logger.info("[%s] Video sent successfully to %s", chat_label, user_label)
    finally:
        cleanup(result.file_path)
        if not send_failed:
            await _safe_delete_message(status_msg)

    if send_failed:
        return
