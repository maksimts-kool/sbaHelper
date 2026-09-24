"""Telegram handlers: links in chats, /start and /stats, and the admin tools."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import UTC, datetime

from telegram import Message, Update, User
from telegram.error import NetworkError, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from sbahelper import cookies, texts
from sbahelper.checks import run_checks
from sbahelper.config import settings
from sbahelper.download import (
    DownloadError,
    DownloadResult,
    LoginRequired,
    Rejected,
    VideoInfo,
    download_video,
)
from sbahelper.links import find_link, is_not_a_video
from sbahelper.stats import DownloadEvent, StatsStore, week_period

log = logging.getLogger(__name__)

STATS_KEY = "stats"
# Rejections ("photo post", "too long") are shown briefly, then removed.
REJECTION_TTL_SEC = 5.0
# Telegram throttles bots that edit one message too often.
MIN_EDIT_INTERVAL_SEC = 1.0


def stats_store(context: ContextTypes.DEFAULT_TYPE) -> StatsStore | None:
    store = context.bot_data.get(STATS_KEY)
    return store if isinstance(store, StatsStore) else None


def is_allowed(chat_id: int) -> bool:
    return not settings.allowed_chat_ids or chat_id in settings.allowed_chat_ids


def _who(chat_id: int, user: User | None) -> str:
    return f"Chat={chat_id} User={user.id if user else 0}"


class StatusMessage:
    """The reply that follows one link: checking → title and progress → sending → gone.

    Download callbacks arrive on a worker thread and are handed to the event
    loop. Edits are serialized, and progress stops for good once a final text
    is shown, so a late progress update can never overwrite it.
    """

    def __init__(self, message: Message) -> None:
        self.message = message
        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._shown = texts.CHECKING
        self._last_edit = time.monotonic()
        self._info: VideoInfo | None = None
        self._percent = 0
        self._live = True

    # Called from the download thread.

    def on_info(self, info: VideoInfo) -> None:
        self._info = info
        asyncio.run_coroutine_threadsafe(self._refresh(), self._loop)

    def on_progress(self, percent: float) -> None:
        step = int(max(0.0, min(percent, 100.0))) // 10 * 10
        if step > self._percent:
            self._percent = step
            asyncio.run_coroutine_threadsafe(self._refresh(), self._loop)

    # Called on the event loop.

    async def _refresh(self) -> None:
        wait = self._last_edit + MIN_EDIT_INTERVAL_SEC - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        async with self._lock:
            if self._live and self._info is not None:
                await self._edit(texts.status(self._info, texts.progress(self._percent)))

    async def show(self, text: str) -> None:
        """Replace the status with a final text; progress updates stop."""
        self._live = False
        async with self._lock:
            await self._edit(text)

    async def flash(self, text: str, seconds: float) -> None:
        await self.show(text)
        await asyncio.sleep(seconds)
        await self.delete()

    async def delete(self, attempts: int = 3) -> None:
        for attempt in range(1, attempts + 1):
            try:
                await self.message.delete()
                return
            except TelegramError as error:
                if "not found" in str(error).lower():
                    return
                if attempt == attempts:
                    log.warning("Status not deleted. Chat=%s Error=%s", self.message.chat_id, error)
                    return
                await asyncio.sleep(attempt)

    async def _edit(self, text: str) -> None:
        if text == self._shown:
            return
        try:
            await self.message.edit_text(text)
        except TelegramError as error:
            if "not modified" not in str(error).lower():
                log.warning("Status not updated. Chat=%s Error=%s", self.message.chat_id, error)
                return
        self._shown = text
        self._last_edit = time.monotonic()


# --------------------------------------------------------------------------- #
#  Links                                                                      #
# --------------------------------------------------------------------------- #


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if message is None or chat is None or not is_allowed(chat.id):
        return
    url = find_link(message.text or message.caption or "")
    if url is None:
        return

    who = _who(chat.id, user)
    if is_not_a_video(url):
        log.info("Profile link ignored. %s URL=%s", who, url)
        return
    log.info("Link received. %s Name=%s URL=%s", who, user.full_name if user else "?", url)

    reply = message.build_reply_arguments(allow_sending_without_reply=True)
    status = StatusMessage(await message.reply_text(texts.CHECKING, do_quote=reply))
    try:
        result = await asyncio.to_thread(
            download_video, url, on_info=status.on_info, on_progress=status.on_progress
        )
    except Rejected as error:
        log.info("Link rejected. %s Reason=%s", who, error.reason)
        context.application.create_task(status.flash(error.text, REJECTION_TTL_SEC))
        return
    except LoginRequired as error:
        log.error(
            "Cookies needed: the site asks to log in. %s URL=%s Error=%s. "
            "Send fresh cookies to the bot in a private chat.",
            who,
            url,
            error.reason,
        )
        await status.show(error.text)
        return
    except DownloadError as error:
        level = logging.ERROR if error.alert else logging.WARNING
        log.log(level, "Download failed. %s URL=%s Error=%s", who, url, error.reason)
        await status.show(error.text)
        return
    except Exception:
        log.exception("Download crashed. %s URL=%s", who, url)
        await status.show(texts.UNEXPECTED)
        return

    try:
        await status.show(texts.status(result.info, texts.SENDING))
        with result.path.open("rb") as video:
            await message.reply_video(
                video,
                caption=texts.caption(result.info),
                duration=result.info.duration or None,
                width=result.info.width,
                height=result.info.height,
                supports_streaming=True,
                do_quote=reply,
            )
    except TelegramError as error:
        log.warning("Video not sent. %s Error=%s", who, error)
        await status.show(texts.send_failed(str(error)))
        return
    finally:
        result.cleanup()

    log.info(
        "Video sent. %s Platform=%s Size=%.1fMB",
        who,
        result.info.platform,
        result.size_bytes / 1024**2,
    )
    await status.delete()
    await _record(context, chat.id, user, result)


async def _record(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: User | None, result: DownloadResult
) -> None:
    store = stats_store(context)
    if store is None:
        return
    info = result.info
    event = DownloadEvent(
        chat_id=chat_id,
        user_id=user.id if user else 0,
        user_name=user.full_name if user else "",
        username=user.username if user else None,
        platform=info.platform,
        duration_sec=info.duration,
        size_bytes=result.size_bytes,
        title=info.title,
        uploader=info.uploader,
        view_count=info.view_count,
    )
    try:
        await asyncio.to_thread(store.record, event)
    except (OSError, sqlite3.Error) as error:
        log.error("Download not recorded. %s Error=%s", _who(chat_id, user), error)


# --------------------------------------------------------------------------- #
#  Commands                                                                   #
# --------------------------------------------------------------------------- #


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if message := update.effective_message:
        text = texts.start(settings.max_duration_sec // 60, settings.max_file_size_mb)
        await message.reply_text(text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None or not is_allowed(chat.id):
        return
    store = stats_store(context)
    if store is None:
        await message.reply_text(texts.STATS_OFF)
        return
    start, end = week_period(datetime.now(settings.tz))
    stats = await asyncio.to_thread(store.weekly_stats, chat_id=chat.id, start=start, end=end)
    await message.reply_text(texts.weekly_stats(stats, title=texts.THIS_WEEK))


async def cmd_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if message := update.effective_message:
        statuses = await asyncio.to_thread(cookies.status, settings.cookies_dir)
        await message.reply_text(texts.cookies_status(statuses, datetime.now(UTC)))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if message := update.effective_message:
        reply = await message.reply_text(texts.CHECKS_RUNNING)
        results = await asyncio.to_thread(run_checks)
        await reply.edit_text(texts.check_results(results))


async def on_cookie_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """An admin sent a file in a private chat: import it as cookies."""
    message, user = update.effective_message, update.effective_user
    if message is None or message.document is None:
        return
    document = message.document
    if (document.file_size or 0) > cookies.MAX_FILE_BYTES:
        await message.reply_text(texts.cookies_rejected("the file is larger than 1 MB"))
        return

    data = await (await document.get_file()).download_as_bytearray()
    source = f"telegram:{user.id if user else 0}"
    try:
        text = bytes(data).decode("utf-8-sig")
        counts = await asyncio.to_thread(
            cookies.import_text, settings.cookies_dir, text, source=source
        )
    except UnicodeDecodeError:
        await message.reply_text(texts.cookies_rejected("not a UTF-8 text file"))
        return
    except cookies.CookieError as error:
        await message.reply_text(texts.cookies_rejected(str(error)))
        return

    # Cookies are credentials: do not leave them in the chat history.
    try:
        await message.delete()
    except TelegramError as error:
        log.warning("Cookie message not deleted. Error=%s", error)
    await message.chat.send_message(texts.cookies_saved(counts))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, NetworkError):
        log.warning("Telegram network error. Error=%s", error)
        return
    update_id = update.update_id if isinstance(update, Update) else None
    log.error("Unhandled error. Update=%s", update_id, exc_info=error)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    if settings.admin_ids:
        admins = filters.User(user_id=settings.admin_ids) & filters.ChatType.PRIVATE
        app.add_handler(CommandHandler("cookies", cmd_cookies, filters=admins))
        app.add_handler(CommandHandler("check", cmd_check, filters=admins))
        app.add_handler(MessageHandler(admins & filters.Document.ALL, on_cookie_file))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
