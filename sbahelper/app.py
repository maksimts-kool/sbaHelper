"""Application wiring and the entrypoint (`python -m sbahelper`)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from telegram import BotCommand, BotCommandScopeChat, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, Defaults

from sbahelper import cookies, handlers, jobs, texts
from sbahelper.alerts import TelegramAlerts
from sbahelper.config import settings
from sbahelper.download import log_impersonation_status
from sbahelper.log import setup_logging
from sbahelper.stats import StatsStore

log = logging.getLogger(__name__)

ALERTS_KEY = "alerts"
# Downloads run in worker threads, so a few links can be handled at once.
CONCURRENT_UPDATES = 8


def _open_stats() -> StatsStore | None:
    if not settings.stats_enabled:
        log.info("Stats disabled. STATS_ENABLED=0")
        return None
    try:
        return StatsStore(settings.stats_db_path)
    except (OSError, sqlite3.Error) as error:
        log.error("Stats database unavailable. Path=%s Error=%s", settings.stats_db_path, error)
        return None


def _prepare_cookies() -> None:
    try:
        settings.cookies_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        log.warning("Cookie directory not created. Path=%s Error=%s", settings.cookies_dir, error)
    cookies.import_dropped(settings.cookies_dir)
    for item in cookies.status(settings.cookies_dir):
        expires = f"{item.login_expires:%Y-%m-%d}" if item.login_expires else "-"
        log.info(
            "Cookies. Platform=%s Count=%d LoggedIn=%s Expires=%s",
            item.platform,
            item.count,
            "yes" if item.logged_in else "no",
            expires,
        )


async def _set_commands(app: Application) -> None:
    """The command menus. Best effort: a hiccup here must not stop the bot."""
    menus = [(texts.COMMANDS, None)] + [
        (texts.ADMIN_COMMANDS, BotCommandScopeChat(admin_id)) for admin_id in settings.admin_ids
    ]
    for commands, scope in menus:
        try:
            await app.bot.set_my_commands([BotCommand(*c) for c in commands], scope=scope)
        except TelegramError as error:
            # An admin's chat exists for Telegram only after they have pressed Start once.
            target = scope.chat_id if scope else "default"
            log.warning("Command menu not set. Chat=%s Error=%s", target, error)


async def _post_init(app: Application) -> None:
    if settings.admin_ids:
        alerts = TelegramAlerts(app.bot, settings.admin_ids, asyncio.get_running_loop())
        logging.getLogger().addHandler(alerts)
        app.bot_data[ALERTS_KEY] = alerts
    else:
        log.warning("ADMIN_IDS is empty: no error alerts, cookie uploads or admin commands.")

    # Everything below may log errors, so it runs after the alerts are installed.
    await asyncio.to_thread(log_impersonation_status)
    app.bot_data[handlers.STATS_KEY] = await asyncio.to_thread(_open_stats)
    jobs.schedule(app)
    await asyncio.to_thread(_prepare_cookies)
    await _set_commands(app)
    log.info(
        "Bot started. Username=@%s ID=%s Chats=%s Admins=%s",
        app.bot.username,
        app.bot.id,
        ",".join(map(str, sorted(settings.allowed_chat_ids))) or "all",
        len(settings.admin_ids),
    )
    if not settings.allowed_chat_ids:
        log.warning("ALLOWED_CHAT_IDS is empty: the bot answers in every chat.")


async def _post_shutdown(app: Application) -> None:
    if alerts := app.bot_data.pop(ALERTS_KEY, None):
        logging.getLogger().removeHandler(alerts)


def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(settings.token)
        .defaults(
            Defaults(
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                tzinfo=settings.tz,
            )
        )
        .concurrent_updates(CONCURRENT_UPDATES)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .media_write_timeout(180)
        .pool_timeout(30)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    handlers.register(app)
    return app


def main() -> int:
    setup_logging(settings.log_level, settings.tz)
    for issue in settings.issues:
        logging.getLogger("sbahelper.config").warning(issue)
    if not settings.token:
        log.critical("DOWNLOADER_BOT_TOKEN is not set.")
        return 1

    build_application().run_polling(
        timeout=30,
        bootstrap_retries=-1,
        drop_pending_updates=True,
        allowed_updates=[Update.MESSAGE],
    )
    return 0
