"""Application wiring and the entrypoint (`python -m sbahelper`).

Long polling stays (https://core.telegram.org/bots/api#getupdates): it needs
no public endpoint, and webhooks bring nothing this bot needs. Shutdown on
SIGTERM/SIGINT: python-telegram-bot stops polling and the application, then
`_post_stop` lets the download service finish or cancel its work within
SHUTDOWN_GRACE_SEC while the bot can still send messages.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import suppress

from telegram import BotCommand, BotCommandScopeChat, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, Defaults

from sbahelper import cookies, handlers, jobs, media, texts
from sbahelper.alerts import TelegramAlerts
from sbahelper.cache import FileCache, policy_fingerprint
from sbahelper.config import settings
from sbahelper.db import Database, SchemaTooNew
from sbahelper.download import (
    MEDIA_POLICY_VERSION,
    cleanup_stale_workdirs,
    log_impersonation_status,
)
from sbahelper.log import setup_logging
from sbahelper.pacing import Pacer
from sbahelper.prefs import Preferences
from sbahelper.runtime import RUNTIME_KEY, Runtime
from sbahelper.service import DownloadService
from sbahelper.stats import StatsStore

log = logging.getLogger(__name__)

ALERTS_KEY = "alerts"
# Handlers only admit and queue work, so a few concurrent updates are plenty.
CONCURRENT_UPDATES = 8
SHUTDOWN_GRACE_SEC = 20.0
ALLOWED_UPDATES = [Update.MESSAGE, Update.CALLBACK_QUERY, Update.MY_CHAT_MEMBER]


def _open_database() -> Database | None:
    if not settings.stats_enabled and not settings.cache_ttl_days:
        log.info("Database disabled. STATS_ENABLED=0 CACHE_TTL_DAYS=0")
        return None
    try:
        return Database(settings.stats_db_path)
    except SchemaTooNew as error:
        log.error("Database refused: %s. Path=%s", error, settings.stats_db_path)
    except (OSError, sqlite3.Error) as error:
        log.error("Database unavailable. Path=%s Error=%s", settings.stats_db_path, error)
    return None


def _prepare_cookies() -> None:
    try:
        cookies.secure(settings.cookies_dir)
    except OSError as error:
        log.warning("Cookie directory not prepared. Path=%s Error=%s", settings.cookies_dir, error)
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


def _check_tools() -> None:
    if missing := media.missing_tools():
        log.error("Video tools missing: every download will fail. Missing=%s", ",".join(missing))


async def _set_commands(app: Application) -> None:
    """The command menus: presentation only, every handler checks rights itself."""
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


def build_runtime(app: Application, db: Database | None) -> Runtime:
    stats = StatsStore(db) if db is not None and settings.stats_enabled else None
    prefs = Preferences(db) if db is not None else None
    cache = None
    if db is not None and settings.cache_ttl_days:
        policy = policy_fingerprint(
            MEDIA_POLICY_VERSION, settings.max_file_size_bytes, settings.max_duration_sec
        )
        # Namespaced by the bot's numeric id: file ids cannot move between bots.
        cache = FileCache(db, bot_id=app.bot.id, policy=policy, ttl_days=settings.cache_ttl_days)
    pacer = app.bot.rate_limiter if isinstance(app.bot.rate_limiter, Pacer) else None
    service = DownloadService(settings, cache=cache, stats=stats, prefs=prefs)
    return Runtime(
        service=service,
        db=db,
        stats=stats,
        cache=cache,
        prefs=prefs,
        pacer=pacer,
        reads_all_group_messages=app.bot.can_read_all_group_messages,
    )


async def _post_init(app: Application) -> None:
    if settings.admin_ids:
        alerts = TelegramAlerts(app.bot, settings.admin_ids, asyncio.get_running_loop())
        logging.getLogger().addHandler(alerts)
        app.bot_data[ALERTS_KEY] = alerts
    else:
        log.warning("ADMIN_IDS is empty: no error alerts, cookie uploads or admin commands.")

    # Everything below may log errors, so it runs after the alerts are installed.
    await asyncio.to_thread(log_impersonation_status)
    await asyncio.to_thread(_check_tools)
    # No worker runs yet, so every leftover work directory is abandoned.
    if removed := await asyncio.to_thread(cleanup_stale_workdirs, settings.temp_dir, 0):
        log.info("Leftover work directories removed. Count=%d", removed)
    db = await asyncio.to_thread(_open_database)
    rt = build_runtime(app, db)
    app.bot_data[RUNTIME_KEY] = rt
    jobs.schedule(app)
    await asyncio.to_thread(_prepare_cookies)
    await _set_commands(app)
    log.info(
        "Bot started. Username=@%s ID=%s Access=%s Admins=%d PrivacyMode=%s "
        "MaxBytes=%d Active=%d Queue=%d Cache=%s",
        app.bot.username,
        app.bot.id,
        settings.describe_access(),
        len(settings.admin_ids),
        "off" if rt.reads_all_group_messages else "on",
        settings.max_file_size_bytes,
        settings.max_active_downloads,
        settings.max_queued_downloads,
        f"{settings.cache_ttl_days}d" if rt.cache else "off",
    )
    if settings.access_policy == "open":
        log.warning("ACCESS_POLICY is open: the bot answers in every chat.")


async def _post_stop(app: Application) -> None:
    rt = app.bot_data.get(RUNTIME_KEY)
    if isinstance(rt, Runtime):
        await rt.service.shutdown(SHUTDOWN_GRACE_SEC)
    if alerts := app.bot_data.get(ALERTS_KEY):
        await alerts.aclose()


async def _post_shutdown(app: Application) -> None:
    if alerts := app.bot_data.pop(ALERTS_KEY, None):
        logging.getLogger().removeHandler(alerts)
    with suppress(OSError):
        settings.heartbeat_path.unlink(missing_ok=True)
    log.info("Bot stopped.")


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
        .rate_limiter(Pacer())
        .concurrent_updates(CONCURRENT_UPDATES)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(60)
        .media_write_timeout(180)
        .pool_timeout(30)
        .post_init(_post_init)
        .post_stop(_post_stop)
        .post_shutdown(_post_shutdown)
        .build()
    )
    handlers.register(app)
    return app


def main() -> int:
    # Everything the bot writes (database, cookies, work files) is private to it.
    os.umask(0o077)
    setup_logging(settings.log_level, settings.tz)
    for issue in settings.issues:
        logging.getLogger("sbahelper.config").warning(issue)
    if not settings.token:
        log.critical("DOWNLOADER_BOT_TOKEN is not set.")
        return 1

    # Telegram keeps undelivered updates for up to 24 h. By default they are
    # dropped on start so a restart does not replay a burst of old links.
    build_application().run_polling(
        timeout=30,
        bootstrap_retries=-1,
        drop_pending_updates=settings.drop_pending_updates,
        allowed_updates=ALLOWED_UPDATES,
    )
    return 0
