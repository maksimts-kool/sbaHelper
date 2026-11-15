"""Telegram handlers. Thin: they parse the update, check who may do what, and
hand the work to `service.DownloadService` or the stores.

Authorization is checked here, in the handler, every time. Command menus and
scopes are presentation only (https://core.telegram.org/bots/features#commands),
and callback data comes from the client, so it is parsed strictly and the
permission is checked again when the button is pressed. Every callback query
is answered, including stale and refused ones
(https://core.telegram.org/bots/api#answercallbackquery).

Three separate gates:

- the bot's allowlist (ACCESS_POLICY / ALLOWED_CHAT_IDS) decides which chats
  are served at all;
- Telegram's Privacy Mode decides which group messages the bot even receives
  (https://core.telegram.org/bots/features#privacy-mode); /download works
  either way because commands are always delivered;
- roles: bot admins (ADMIN_IDS) run /status, /cookies, /check and upload
  cookies in private chats; group settings belong to the group's Telegram
  administrators; personal settings to the person.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import datetime

from telegram import (
    CallbackQuery,
    Chat,
    ChatMember,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from telegram.error import Conflict, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from sbahelper import cookies, texts
from sbahelper.checks import last_results, run_checks
from sbahelper.config import settings
from sbahelper.links import Link, message_links
from sbahelper.runtime import Runtime, runtime
from sbahelper.service import Refused, Request, StatusMessage
from sbahelper.stats import PERIODS, period_bounds

log = logging.getLogger(__name__)

# The account Telegram shows for anonymous group admins; it cannot be verified.
ANONYMOUS_ADMIN_ID = 1087968824
# Cookie uploads: far below getFile's 20 MB, and a size must be advertised.
MAX_COOKIE_BYTES = cookies.MAX_FILE_BYTES
_CALLBACK = re.compile(
    r"st:(?P<period>day|week|month)"
    r"|ps:stats:(?P<counted>[01]):(?P<ps_user>\d{1,20})"
    r"|gs:auto:(?P<auto>[01])"
    r"|gs:topic:(?P<topic>here|main)"
    r"|dd:(?P<answer>yes|no):(?P<dd_user>\d{1,20})"
)


def is_allowed(chat_id: int) -> bool:
    return settings.chat_allowed(chat_id)


def is_bot_admin(user: User | None) -> bool:
    return user is not None and user.id in settings.admin_ids


def _who(chat_id: int, user: User | None) -> str:
    return f"Chat={chat_id} User={user.id if user else 0}"


async def _reply(message: Message, text: str, **kwargs: object) -> Message | None:
    try:
        return await message.reply_text(text, **kwargs)
    except TelegramError as error:
        log.warning("Reply not sent. Chat=%s Error=%s", message.chat_id, error)
        return None


# --------------------------------------------------------------------------- #
#  Links                                                                      #
# --------------------------------------------------------------------------- #


async def _auto_download_enabled(rt: Runtime, chat: Chat) -> bool:
    if chat.type == Chat.PRIVATE or rt.prefs is None:
        return True
    try:
        return (await asyncio.to_thread(rt.prefs.chat, chat.id)).auto_download
    except sqlite3.Error as error:
        log.warning("Chat settings unreadable. Chat=%s Error=%s", chat.id, error)
        return True


async def handle_links(
    update: Update, context: ContextTypes.DEFAULT_TYPE, links: list[Link], *, source: Message
) -> None:
    """Admit and submit each link; `source` is the message the video replies to."""
    rt = runtime(context)
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if rt is None or message is None or chat is None:
        return
    who = _who(chat.id, user)
    limit = settings.max_links_per_message
    if len(links) > limit:
        log.info("Extra links skipped. %s Found=%d Limit=%d", who, len(links), limit)
        await _reply(message, texts.too_many_links(limit))
        links = links[:limit]

    reply_to = source.build_reply_arguments(allow_sending_without_reply=True)
    for link in links:
        log.info("Link received. %s Platform=%s URL=%s", who, link.platform, link.url)
        try:
            ticket = rt.service.admit(chat.id, user.id if user else 0)
        except Refused as refusal:
            log.info("Link refused. %s Reason=%s", who, refusal.code)
            await _reply(source, texts.refusal(refusal.code), do_quote=reply_to)
            return
        status = await _reply(source, texts.CHECKING, do_quote=reply_to)
        if status is None:
            ticket.release()
            return
        request = Request(
            link=link,
            message=source,
            status=StatusMessage(status),
            ticket=ticket,
            chat_id=chat.id,
            user_id=user.id if user else 0,
            user_name=user.full_name if user else "",
            username=user.username if user else None,
        )
        try:
            await rt.service.submit(request)
        except Exception:
            ticket.release()
            raise


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Passive detection: links in ordinary messages of allowed chats."""
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None or not is_allowed(chat.id):
        return
    links = message_links(message)
    if not links:
        return
    rt = runtime(context)
    if rt is None or not await _auto_download_enabled(rt, chat):
        return
    await handle_links(update, context, links, source=message)


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/download <link>, or /download as a reply to a message with links.

    Works with Privacy Mode on: commands always reach the bot.
    """
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None or not is_allowed(chat.id):
        return
    links = message_links(message)
    source = message
    if not links and message.reply_to_message is not None:
        source = message.reply_to_message
        links = message_links(source)
    if not links:
        has_args = bool(context.args)
        await _reply(
            message, texts.NO_LINKS if has_args or source is not message else texts.DOWNLOAD_USAGE
        )
        return
    await handle_links(update, context, links, source=source)


# --------------------------------------------------------------------------- #
#  General commands                                                           #
# --------------------------------------------------------------------------- #


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None or not is_allowed(chat.id):
        return
    await _reply(
        message, texts.start(settings.max_duration_sec // 60, settings.max_file_size_bytes)
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None or not is_allowed(chat.id):
        return
    rt = runtime(context)
    auto = await _auto_download_enabled(rt, chat) if rt else True
    text = texts.help_text(
        max_links=settings.max_links_per_message,
        is_group=chat.type != Chat.PRIVATE,
        auto_download=auto,
    )
    await _reply(message, text)


def stats_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ("• " if period == current else "") + texts.PERIOD_BUTTONS[period],
                    callback_data=f"st:{period}",
                )
                for period in PERIODS
            ]
        ]
    )


async def _stats_text(rt: Runtime, chat_id: int, period: str) -> str:
    assert rt.stats is not None
    start, end = period_bounds(period, datetime.now(settings.tz))
    stats = await asyncio.to_thread(rt.stats.period_stats, chat_id=chat_id, start=start, end=end)
    return texts.period_stats(stats, period=period)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None or not is_allowed(chat.id):
        return
    rt = runtime(context)
    if rt is None or rt.stats is None:
        await _reply(message, texts.STATS_OFF)
        return
    arg = context.args[0].lower() if context.args else "week"
    period = {"день": "day", "неделя": "week", "месяц": "month"}.get(arg, arg)
    if period not in PERIODS:
        period = "week"
    try:
        text = await _stats_text(rt, chat.id, period)
    except sqlite3.Error as error:
        log.error("Stats not read. Chat=%s Error=%s", chat.id, error)
        await _reply(message, texts.DATA_UNAVAILABLE)
        return
    await _reply(message, text, reply_markup=stats_keyboard(period))


async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None or not is_allowed(chat.id):
        return
    await _reply(
        message,
        texts.privacy(
            retention_days=settings.stats_retention_days, cache_days=settings.cache_ttl_days
        ),
    )


# --------------------------------------------------------------------------- #
#  Settings                                                                   #
# --------------------------------------------------------------------------- #


def personal_keyboard(user_id: int, counted: bool) -> InlineKeyboardMarkup:
    toggle = "0" if counted else "1"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    texts.btn_stats(counted), callback_data=f"ps:stats:{toggle}:{user_id}"
                )
            ]
        ]
    )


def group_keyboard(auto: bool, summary_topic: int | None, in_topic: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(texts.btn_auto(auto), callback_data=f"gs:auto:{int(not auto)}")]]
    if in_topic:
        rows.append([InlineKeyboardButton(texts.BTN_SUMMARY_HERE, callback_data="gs:topic:here")])
    if summary_topic:
        rows.append([InlineKeyboardButton(texts.BTN_SUMMARY_MAIN, callback_data="gs:topic:main")])
    return InlineKeyboardMarkup(rows)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if message is None or chat is None or not is_allowed(chat.id):
        return
    rt = runtime(context)
    if rt is None or rt.prefs is None:
        await _reply(message, texts.DATA_UNAVAILABLE)
        return
    try:
        if chat.type == Chat.PRIVATE:
            if user is None:
                return
            mine = await asyncio.to_thread(rt.prefs.user, user.id)
            counted = not mine.stats_opt_out
            await _reply(
                message,
                texts.personal_settings(counted=counted),
                reply_markup=personal_keyboard(user.id, counted),
            )
            return
        group = await asyncio.to_thread(rt.prefs.chat, chat.id)
    except sqlite3.Error as error:
        log.error("Settings not read. %s Error=%s", _who(chat.id, user), error)
        await _reply(message, texts.DATA_UNAVAILABLE)
        return
    in_topic = bool(message.is_topic_message)
    await _reply(
        message,
        texts.group_settings(
            auto_download=group.auto_download,
            summary_topic=group.summary_thread_id,
            in_topic=in_topic,
        ),
        reply_markup=group_keyboard(group.auto_download, group.summary_thread_id, in_topic),
    )


async def is_group_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError as error:
        log.warning("Admin check failed. Chat=%s User=%s Error=%s", chat_id, user_id, error)
        return False
    return member.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)


# --------------------------------------------------------------------------- #
#  Personal data                                                              #
# --------------------------------------------------------------------------- #


def delete_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(texts.BTN_DELETE, callback_data=f"dd:yes:{user_id}"),
                InlineKeyboardButton(texts.BTN_CANCEL, callback_data=f"dd:no:{user_id}"),
            ]
        ]
    )


async def cmd_delete_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if message is None or chat is None or user is None or not is_allowed(chat.id):
        return
    rt = runtime(context)
    if rt is None or rt.stats is None:
        await _reply(message, texts.DATA_UNAVAILABLE)
        return
    try:
        count = await asyncio.to_thread(rt.stats.user_row_count, user.id)
    except sqlite3.Error as error:
        log.error("Personal data not counted. User=%s Error=%s", user.id, error)
        await _reply(message, texts.DATA_UNAVAILABLE)
        return
    await _reply(message, texts.delete_confirm(count), reply_markup=delete_keyboard(user.id))


# --------------------------------------------------------------------------- #
#  Callback queries                                                           #
# --------------------------------------------------------------------------- #


async def _answer(query: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=alert)
    except TelegramError as error:
        log.debug("Callback not answered. Error=%s", error)


async def _edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except TelegramError as error:
        if "not modified" not in str(error).lower():
            log.warning("Callback message not edited. Error=%s", error)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    user = query.from_user
    chat = query.message.chat if query.message is not None else None
    match = _CALLBACK.fullmatch(query.data or "")
    if match is None or chat is None:
        await _answer(query, texts.STALE_BUTTON)
        return
    rt = runtime(context)
    if not is_allowed(chat.id) or rt is None:
        await _answer(query, texts.NOT_AVAILABLE)
        return
    try:
        await _dispatch_callback(query, context, rt, match, chat, user)
    except sqlite3.Error as error:
        log.error("Callback failed on the database. %s Error=%s", _who(chat.id, user), error)
        await _answer(query, texts.DATA_UNAVAILABLE, alert=True)


async def _dispatch_callback(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    rt: Runtime,
    match: re.Match[str],
    chat: Chat,
    user: User,
) -> None:
    if period := match["period"]:
        if rt.stats is None:
            await _answer(query, texts.STATS_OFF)
            return
        await _answer(query)
        await _edit(query, await _stats_text(rt, chat.id, period), stats_keyboard(period))
        return

    if match["ps_user"] is not None:
        if int(match["ps_user"]) != user.id or chat.type != Chat.PRIVATE:
            await _answer(query, texts.NOT_YOURS)
            return
        if rt.prefs is None:
            await _answer(query, texts.DATA_UNAVAILABLE)
            return
        counted_next = match["counted"] == "1"  # the button carries the new state
        saved = await asyncio.to_thread(rt.prefs.set_stats_opt_out, user.id, not counted_next)
        counted = not saved.stats_opt_out
        await _answer(query, texts.SAVED)
        await _edit(
            query, texts.personal_settings(counted=counted), personal_keyboard(user.id, counted)
        )
        return

    if match["auto"] is not None or match["topic"] is not None:
        if chat.type == Chat.PRIVATE or rt.prefs is None:
            await _answer(query, texts.STALE_BUTTON)
            return
        if user.id == ANONYMOUS_ADMIN_ID:
            await _answer(query, texts.ANONYMOUS_ADMIN, alert=True)
            return
        # Checked at press time: the keyboard may be old and roles change.
        if not await is_group_admin(context, chat.id, user.id):
            await _answer(query, texts.NOT_GROUP_ADMIN, alert=True)
            return
        if match["auto"] is not None:
            changes: dict[str, object] = {"auto_download": match["auto"] == "1"}
        else:
            message = query.message
            here = (
                message.message_thread_id
                if isinstance(message, Message) and message.is_topic_message
                else None
            )
            changes = {"summary_thread_id": here if match["topic"] == "here" else None}
        group = await asyncio.to_thread(rt.prefs.update_chat, chat.id, **changes)
        log.info("Group settings changed. %s Changes=%s", _who(chat.id, user), changes)
        in_topic = isinstance(query.message, Message) and bool(query.message.is_topic_message)
        await _answer(query, texts.SAVED)
        await _edit(
            query,
            texts.group_settings(
                auto_download=group.auto_download,
                summary_topic=group.summary_thread_id,
                in_topic=in_topic,
            ),
            group_keyboard(group.auto_download, group.summary_thread_id, in_topic),
        )
        return

    if match["dd_user"] is not None:
        if int(match["dd_user"]) != user.id:
            await _answer(query, texts.NOT_YOURS, alert=True)
            return
        if match["answer"] == "no":
            await _answer(query)
            await _edit(query, texts.DELETE_CANCELLED, None)
            return
        if rt.stats is None:
            await _answer(query, texts.DATA_UNAVAILABLE)
            return
        removed = await asyncio.to_thread(rt.stats.delete_user, user.id)
        log.info("Personal data deleted. User=%s Rows=%d", user.id, removed)
        await _answer(query)
        await _edit(query, texts.delete_done(removed), None)
        return

    await _answer(query, texts.STALE_BUTTON)


# --------------------------------------------------------------------------- #
#  Bot admins                                                                 #
# --------------------------------------------------------------------------- #


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    rt = runtime(context)
    if message is None or rt is None:
        return
    database_ok = await asyncio.to_thread(rt.db.ping) if rt.db is not None else None
    checks = await asyncio.to_thread(last_results, rt.db)
    status = await asyncio.to_thread(
        rt.service.status,
        database_ok=database_ok,
        privacy_mode=rt.privacy_mode(),
        flood_waits=rt.pacer.flood_waits if rt.pacer else 0,
        throttled=rt.pacer.skipped if rt.pacer else 0,
        last_checks=checks,
    )
    await _reply(message, texts.bot_status(status, datetime.now(settings.tz)))


async def cmd_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if message := update.effective_message:
        statuses = await asyncio.to_thread(cookies.status, settings.cookies_dir)
        await _reply(message, texts.cookies_status(statuses, datetime.now(settings.tz)))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    rt = runtime(context)
    reply = await _reply(message, texts.CHECKS_RUNNING)
    results = await asyncio.to_thread(run_checks, rt.db if rt else None)
    if reply is not None:
        try:
            await reply.edit_text(texts.check_results(results))
        except TelegramError as error:
            log.warning("Check results not shown. Error=%s", error)


async def on_cookie_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """An admin sent a file in a private chat: import it as cookies, then delete it."""
    message, user = update.effective_message, update.effective_user
    if message is None or message.document is None:
        return
    document = message.document
    # An unknown size is not a small size: refuse rather than download blindly.
    if document.file_size is None:
        await _reply(message, texts.cookies_rejected("the file size is unknown"))
        return
    if document.file_size > MAX_COOKIE_BYTES:
        await _reply(message, texts.cookies_rejected("the file is larger than 1 MB"))
        return

    try:
        data = bytes(await (await document.get_file()).download_as_bytearray())
    except TelegramError as error:
        log.warning("Cookie file not downloaded. User=%s Error=%s", user.id if user else 0, error)
        await _reply(message, texts.cookies_rejected("Telegram did not hand over the file"))
        return
    if len(data) > MAX_COOKIE_BYTES:
        await _reply(message, texts.cookies_rejected("the file is larger than 1 MB"))
        return

    source = f"telegram:{user.id if user else 0}"
    try:
        text = data.decode("utf-8-sig")
        counts = await asyncio.to_thread(
            cookies.import_text, settings.cookies_dir, text, source=source
        )
    except UnicodeDecodeError:
        await _reply(message, texts.cookies_rejected("not a UTF-8 text file"))
        return
    except cookies.CookieError as error:
        await _reply(message, texts.cookies_rejected(str(error)))
        return
    except OSError as error:
        log.error("Cookies not saved. Error=%s", error)
        await _reply(message, texts.cookies_rejected("the cookie folder is not writable"))
        return

    # Cookies are credentials: do not leave them in the chat history. Deleting
    # the message removes it from this chat; it cannot recall copies elsewhere.
    deleted = True
    try:
        await message.delete()
    except TelegramError as error:
        deleted = False
        log.warning("Cookie message not deleted. Error=%s", error)
    try:
        await message.chat.send_message(texts.cookies_saved(counts, deleted=deleted))
    except TelegramError as error:
        log.warning("Cookie confirmation not sent. Error=%s", error)


# --------------------------------------------------------------------------- #
#  Membership, migration, errors                                              #
# --------------------------------------------------------------------------- #


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track whether the bot is still in a chat, to stop scheduled messages there."""
    change: ChatMemberUpdated | None = update.my_chat_member
    rt = runtime(context)
    if change is None or rt is None or rt.prefs is None:
        return
    status = change.new_chat_member.status
    active = status not in (ChatMember.LEFT, ChatMember.BANNED)
    try:
        await asyncio.to_thread(rt.prefs.update_chat, change.chat.id, active=active)
    except sqlite3.Error as error:
        log.warning("Membership not saved. Chat=%s Error=%s", change.chat.id, error)
        return
    log.info("Bot membership changed. Chat=%s Status=%s", change.chat.id, status)


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A group became a supergroup: carry statistics and settings to the new id."""
    message = update.effective_message
    rt = runtime(context)
    if message is None or rt is None or rt.stats is None or not message.migrate_to_chat_id:
        return
    old, new = message.chat_id, message.migrate_to_chat_id
    try:
        moved = await asyncio.to_thread(rt.stats.migrate_chat, old, new)
    except sqlite3.Error as error:
        log.error("Chat migration not applied. Old=%s New=%s Error=%s", old, new, error)
        return
    log.info("Chat migrated. Old=%s New=%s Rows=%d", old, new, moved)
    if settings.access_policy == "allowlist" and old in settings.allowed_chat_ids:
        log.warning(
            "Allowed chat became a supergroup: add the new id to ALLOWED_CHAT_IDS. Old=%s New=%s",
            old,
            new,
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, Conflict):
        log.error(
            "Another bot instance is polling with the same token. Stop the duplicate. Error=%s",
            error,
        )
        return
    if isinstance(error, NetworkError):
        log.warning("Telegram network error. Error=%s", error)
        return
    update_id = update.update_id if isinstance(update, Update) else None
    log.error("Unhandled error. Update=%s", update_id, exc_info=error)


def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("delete_my_data", cmd_delete_my_data))
    if settings.admin_ids:
        admins = filters.User(user_id=settings.admin_ids) & filters.ChatType.PRIVATE
        app.add_handler(CommandHandler("status", cmd_status, filters=admins))
        app.add_handler(CommandHandler("cookies", cmd_cookies, filters=admins))
        app.add_handler(CommandHandler("check", cmd_check, filters=admins))
        app.add_handler(MessageHandler(admins & filters.Document.ALL, on_cookie_file))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
