"""
Downloader Bot: Telegram application, message handling, progress tracking,
and the container entrypoint (startup link checks, then polling).
Listens for TikTok and YouTube links.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from telegram import BotCommand, Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from downloader.config import (
    ALLOWED_CHAT_IDS,
    DOWNLOADER_BOT_TOKEN,
    MAX_FILE_SIZE_MB,
    MAX_SHORT_DURATION_SEC,
    STARTUP_CHECKS_REQUIRED,
    STATS_DB_PATH,
    STATS_ENABLED,
    STATS_RETENTION_DAYS,
    STATS_TIMEZONE,
    STATS_WEEKLY_AT,
    STATS_WEEKLY_WEEKDAY,
    capture_exception,
    detect_platform,
    extract_supported_url,
    flush_error_tracking,
    init_error_tracking,
    is_non_video_url,
    is_transient_network_error,
    print_results,
    result_exit_code,
    run_checks,
)
from downloader.config import (
    configure_logging as configure_startup_logging,
)
from downloader.download import (
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
    build_weekly_stats_message,
    escape_md_v2,
    format_duration,
)
from downloader.stats import (
    DownloadEvent,
    StatsStore,
    resolve_timezone,
    sunday_based_weekday,
    week_period,
)
from shared import configure_logging

logger = logging.getLogger(__name__)

# Через сколько секунд удалять сообщение с ошибкой проверки ссылки.
REJECTION_DELETE_DELAY_SEC = 5.0

STATS_STORE_KEY = "stats_store"
STATS_TZ = resolve_timezone(STATS_TIMEZONE)


# --------------------------------------------------------------------------- #
#  Прогресс загрузки                                                           #
# --------------------------------------------------------------------------- #


class ProgressStage:
    def __init__(self, stage: str = "download") -> None:
        self._stage = stage
        self._version = 0
        self._lock = threading.Lock()

    def snapshot(self) -> tuple[str, int]:
        with self._lock:
            return self._stage, self._version

    def is_current(self, stage: str, version: int) -> bool:
        with self._lock:
            return self._stage == stage and self._version == version

    def advance(self, stage: str) -> None:
        with self._lock:
            self._stage = stage
            self._version += 1


def rounded_progress_percent(raw_percent: float) -> int:
    bounded = min(100, max(0, int(raw_percent)))
    return (bounded // 10) * 10


# --------------------------------------------------------------------------- #
#  Статистика                                                                  #
# --------------------------------------------------------------------------- #


def open_stats_store() -> StatsStore | None:
    """Открывает базу статистики. Если не вышло — бот работает без статистики."""
    if not STATS_ENABLED:
        logger.info("Download statistics are disabled (STATS_ENABLED=0).")
        return None

    try:
        return StatsStore(STATS_DB_PATH)
    except (OSError, sqlite3.Error) as e:
        capture_exception(e)
        logger.error("Could not open the statistics database %s: %s", STATS_DB_PATH, e)
        return None


def get_stats_store(context: ContextTypes.DEFAULT_TYPE) -> StatsStore | None:
    store = context.bot_data.get(STATS_STORE_KEY)
    return store if isinstance(store, StatsStore) else None


async def record_download(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    user,
    url: str,
    info,
    size_bytes: int,
) -> None:
    """Записывает отправленное видео. Ошибка записи не должна ломать загрузку."""
    store = get_stats_store(context)
    if store is None:
        return

    event = DownloadEvent(
        chat_id=chat_id,
        user_id=user.id if user else 0,
        user_name=user.full_name if user else "Неизвестно",
        platform=detect_platform(url),
        duration_sec=info.duration or 0,
        size_bytes=size_bytes,
        title=info.title,
        uploader=info.uploader,
        view_count=info.view_count,
    )

    try:
        await asyncio.to_thread(store.record, event)
    except Exception as e:
        capture_exception(e)
        logger.warning("Could not record download statistics: %s", e)


async def _send_weekly_stats(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в неделю: каждому чату — его собственная сводка. Тихие чаты пропускаем."""
    store = get_stats_store(context)
    if store is None:
        return

    start, end = week_period(datetime.now(STATS_TZ))

    try:
        chat_ids = await asyncio.to_thread(store.active_chat_ids, start, end)
    except sqlite3.Error as e:
        capture_exception(e)
        logger.error("Could not read weekly statistics: %s", e)
        return

    for chat_id in chat_ids:
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            continue

        stats = await asyncio.to_thread(store.weekly_stats, chat_id=chat_id, start=start, end=end)
        if not stats.total_downloads:
            continue

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=build_weekly_stats_message(stats),
                parse_mode="HTML",
            )
        except TelegramError as e:
            logger.warning("Could not send weekly statistics to chat %s: %s", chat_id, e)
        else:
            logger.info(
                "Sent weekly statistics to chat %s (%d downloads).",
                chat_id,
                stats.total_downloads,
            )

    if STATS_RETENTION_DAYS:
        cutoff = end - timedelta(days=STATS_RETENTION_DAYS)
        try:
            removed = await asyncio.to_thread(store.prune, older_than=cutoff)
        except sqlite3.Error as e:
            logger.warning("Could not prune old statistics: %s", e)
        else:
            if removed:
                logger.info("Pruned %d statistics rows older than %s.", removed, cutoff.date())


def schedule_weekly_stats(application) -> None:
    if application.bot_data.get(STATS_STORE_KEY) is None:
        return

    job_queue = application.job_queue
    if job_queue is None:
        logger.warning("JobQueue is unavailable; weekly statistics will not be sent.")
        return

    send_at = STATS_WEEKLY_AT.replace(tzinfo=STATS_TZ)
    job_queue.run_daily(
        _send_weekly_stats,
        time=send_at,
        days=(sunday_based_weekday(STATS_WEEKLY_WEEKDAY),),
        name="weekly-stats",
    )
    logger.info(
        "Weekly statistics scheduled: weekday %s at %s (%s).",
        STATS_WEEKLY_WEEKDAY,
        STATS_WEEKLY_AT.strftime("%H:%M"),
        STATS_TIMEZONE,
    )


# --------------------------------------------------------------------------- #
#  Обработчики сообщений                                                       #
# --------------------------------------------------------------------------- #


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
    context.application.create_task(_show_then_delete(status_msg, text, bot=context.bot))


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
    if chat is None:
        logger.debug("Ignored message without an effective chat.")
        return

    user_label = f"{user.full_name} (id={user.id})" if user else "unknown"
    chat_label = f"{chat.title or chat.type} (id={chat.id})"
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
        info = await loop.run_in_executor(None, fetch_info, url)
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
    # Размер нужен для статистики — файл удаляется сразу после отправки.
    try:
        file_size_bytes = os.path.getsize(result.file_path)
    except OSError:
        file_size_bytes = 0

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

    await record_download(
        context,
        chat_id=chat_id,
        user=user,
        url=url,
        info=result.info,
        size_bytes=file_size_bytes,
    )


# --------------------------------------------------------------------------- #
#  Приложение и точка входа                                                    #
# --------------------------------------------------------------------------- #


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    configure_logging(level_name, quiet_loggers=("yt_dlp", "httpx", "httpcore"))


async def _cmd_start(update, context) -> None:
    await update.message.reply_text(
        "Привет! Я скачиваю <b>короткие</b> видео из <b>TikTok</b> "
        "и <b>YouTube Shorts</b>.\n\n"
        "Просто отправь мне ссылку на видео с любого из этих сайтов:\n"
        "- <code>https://www.tiktok.com/...</code>\n"
        "- <code>https://youtube.com/shorts/...</code>\n\n"
        "Я проверю ссылку и скачаю видео, если это короткий вертикальный ролик "
        f"(до {MAX_SHORT_DURATION_SEC // 60} мин). Горизонтальные и длинные видео, "
        "фото- и обычные посты не поддерживаются.\n\n"
        f"Размер файла - до {MAX_FILE_SIZE_MB} МБ.",
        parse_mode="HTML",
    )


async def _cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    if ALLOWED_CHAT_IDS and chat.id not in ALLOWED_CHAT_IDS:
        return

    store = get_stats_store(context)
    if store is None:
        await message.reply_text("Статистика сейчас не ведётся.")
        return

    start, end = week_period(datetime.now(STATS_TZ))
    stats = await asyncio.to_thread(store.weekly_stats, chat_id=chat.id, start=start, end=end)
    await message.reply_text(
        build_weekly_stats_message(stats, title="Текущая неделя"),
        parse_mode="HTML",
    )


async def _post_init(app) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Информация о боте"),
            BotCommand("stats", "Статистика за текущую неделю"),
        ]
    )
    me = await app.bot.get_me()
    logger.info("Downloader bot started: @%s (id=%s)", me.username, me.id)
    if ALLOWED_CHAT_IDS:
        logger.info("Allowed chats: %s", ALLOWED_CHAT_IDS)
    else:
        logger.warning("ALLOWED_CHAT_IDS is empty; bot responds everywhere.")


async def _error_handler(update: object, context) -> None:
    if context.error is not None and is_transient_network_error(context.error):
        logger.warning("Transient Telegram network error: %s", context.error)
        return

    exc_info = None
    if context.error is not None:
        exc_info = (type(context.error), context.error, context.error.__traceback__)
        capture_exception(context.error)
    logger.error("Unhandled downloader bot error", exc_info=exc_info)


def run_bot() -> int:
    _configure_logging()
    init_error_tracking("downloader-bot")

    if not DOWNLOADER_BOT_TOKEN:
        logger.critical("DOWNLOADER_BOT_TOKEN is not set. Exiting.")
        return 1

    polling_timeout = 30
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=60.0,
        write_timeout=60.0,
        connect_timeout=20.0,
        pool_timeout=20.0,
    )
    polling_request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=float(polling_timeout + 15),
        write_timeout=60.0,
        connect_timeout=20.0,
        pool_timeout=20.0,
    )

    application = (
        ApplicationBuilder()
        .token(DOWNLOADER_BOT_TOKEN)
        .request(request)
        .get_updates_request(polling_request)
        .post_init(_post_init)
        .build()
    )

    application.bot_data[STATS_STORE_KEY] = open_stats_store()
    schedule_weekly_stats(application)

    application.add_handler(CommandHandler("start", _cmd_start))
    application.add_handler(CommandHandler("stats", _cmd_stats))
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    application.add_error_handler(_error_handler)
    application.run_polling(
        timeout=polling_timeout,
        bootstrap_retries=-1,
        drop_pending_updates=True,
    )
    return 0


def run_startup_then_bot() -> int:
    configure_startup_logging()
    init_error_tracking("downloader-startup")

    logger.info("Running startup link checks.")
    results = run_checks()
    print_results(results)
    check_exit_code = result_exit_code(results)
    flush_error_tracking()

    if check_exit_code != 0:
        if STARTUP_CHECKS_REQUIRED:
            logger.error("Startup link checks failed with exit code %s.", check_exit_code)
            return check_exit_code
        logger.warning(
            "Startup link checks failed with exit code %s. "
            "Continuing because STARTUP_CHECKS_REQUIRED=0.",
            check_exit_code,
        )

    return run_bot()


def main() -> int:
    return run_startup_then_bot()


if __name__ == "__main__":
    raise SystemExit(main())
