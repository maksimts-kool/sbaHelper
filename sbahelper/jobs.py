"""Scheduled jobs: weekly summary, daily maintenance, cookie expiry check and
refresh, startup checks and the liveness heartbeat."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, time, timedelta

from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError
from telegram.ext import Application, ContextTypes

from sbahelper import cookies, health, texts
from sbahelper.checks import run_checks
from sbahelper.config import settings
from sbahelper.download import DownloadError, LoginRequired, cleanup_stale_workdirs, refresh
from sbahelper.prefs import ChatSettings
from sbahelper.runtime import Runtime, runtime
from sbahelper.stats import week_period

log = logging.getLogger(__name__)

# Remind admins this long before login cookies expire.
COOKIE_WARNING = timedelta(days=3)
COOKIE_CHECK_AT = time(12)
MAINTENANCE_AT = time(4, 30)
STARTUP_CHECK_DELAY_SEC = 5
HEARTBEAT_EVERY_SEC = 30


async def _chat_settings(rt: Runtime, chat_id: int) -> ChatSettings:
    if rt.prefs is None:
        return ChatSettings(chat_id)
    try:
        return await asyncio.to_thread(rt.prefs.chat, chat_id)
    except sqlite3.Error:
        return ChatSettings(chat_id)


async def _send_summary(
    context: ContextTypes.DEFAULT_TYPE, rt: Runtime, chat_id: int, text: str
) -> bool:
    """Send to the chat's chosen topic, falling back to the main chat if it is gone."""
    chat = await _chat_settings(rt, chat_id)
    thread = chat.summary_thread_id
    try:
        try:
            await context.bot.send_message(chat_id, text, message_thread_id=thread)
        except BadRequest as error:
            if thread is None or "thread" not in str(error).lower():
                raise
            log.warning("Summary topic gone; using the main chat. Chat=%s", chat_id)
            if rt.prefs is not None:
                await asyncio.to_thread(rt.prefs.update_chat, chat_id, summary_thread_id=None)
            await context.bot.send_message(chat_id, text)
    except ChatMigrated as error:
        new_id = error.new_chat_id
        if rt.stats is not None:
            await asyncio.to_thread(rt.stats.migrate_chat, chat_id, new_id)
        log.info("Chat migrated while sending. Old=%s New=%s", chat_id, new_id)
        await context.bot.send_message(new_id, text)
    except Forbidden as error:
        # Removed or blocked: do not try again every week.
        log.warning("Weekly stats not sent; chat marked inactive. Chat=%s Error=%s", chat_id, error)
        if rt.prefs is not None:
            await asyncio.to_thread(rt.prefs.update_chat, chat_id, active=False)
        return False
    return True


async def send_weekly_stats(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Each active chat gets its own summary; chats without downloads get nothing."""
    rt = runtime(context)
    if rt is None or rt.stats is None:
        return
    start, end = week_period(datetime.now(settings.tz))
    try:
        chat_ids = await asyncio.to_thread(rt.stats.active_chat_ids, start, end)
    except sqlite3.Error as error:
        log.error("Weekly stats not read. Error=%s", error)
        return

    for chat_id in filter(settings.chat_allowed, chat_ids):
        try:
            stats = await asyncio.to_thread(
                rt.stats.period_stats, chat_id=chat_id, start=start, end=end
            )
            sent = await _send_summary(context, rt, chat_id, texts.weekly_stats(stats))
        except (TelegramError, sqlite3.Error) as error:
            log.warning("Weekly stats not sent. Chat=%s Error=%s", chat_id, error)
        else:
            if sent:
                log.info("Weekly stats sent. Chat=%s Downloads=%d", chat_id, stats.total_downloads)


async def maintenance(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily, independent of the summary: retention, cache expiry, stray work dirs."""
    rt = runtime(context)
    if rt is not None and rt.stats is not None and settings.stats_retention_days:
        cutoff = datetime.now(UTC) - timedelta(days=settings.stats_retention_days)
        try:
            removed = await asyncio.to_thread(rt.stats.prune, older_than=cutoff)
        except sqlite3.Error as error:
            log.warning("Old stats not pruned. Error=%s", error)
        else:
            if removed:
                log.info("Old stats pruned. Rows=%d Before=%s", removed, cutoff.date())
    if rt is not None and rt.cache is not None:
        try:
            expired = await asyncio.to_thread(rt.cache.prune)
        except sqlite3.Error as error:
            log.warning("Cache not pruned. Error=%s", error)
        else:
            if expired:
                log.info("Expired cache entries removed. Rows=%d", expired)
    # Only directories older than any possible live worker.
    stale = await asyncio.to_thread(
        cleanup_stale_workdirs, settings.temp_dir, settings.download_timeout_sec * 4
    )
    if stale:
        log.warning("Abandoned work directories removed. Count=%d", stale)


def cookie_problems(statuses: list[cookies.CookieStatus], now: datetime) -> list[str]:
    problems = []
    for item in statuses:
        if item.login_expires is None:
            continue
        if not item.logged_in:
            problems.append(f"{item.platform} login expired {item.login_expires:%Y-%m-%d}")
        elif item.login_expires - now < COOKIE_WARNING:
            problems.append(
                f"{item.platform} login expires {item.login_expires:%Y-%m-%d %H:%M} UTC"
            )
    return problems


async def check_cookie_expiry(context: ContextTypes.DEFAULT_TYPE) -> None:
    statuses = await asyncio.to_thread(cookies.status, settings.cookies_dir)
    if problems := cookie_problems(statuses, datetime.now(UTC)):
        log.error(
            "Cookies need attention: %s. Send fresh cookies to the bot in a private chat.",
            "; ".join(problems),
        )


def refresh_cookies_now() -> None:
    """Visit each CHECK_*_URL with its platform's cookies so the session stays in use."""
    for platform, url in settings.check_urls.items():
        if not url or not cookies.platform_file(settings.cookies_dir, platform).is_file():
            continue
        try:
            refresh(url)
        except LoginRequired as error:
            log.error(
                "Cookies rejected: the site asks to log in. Platform=%s Error=%s. "
                "Send fresh cookies to the bot in a private chat.",
                platform,
                error.reason,
            )
        except DownloadError as error:
            level = logging.ERROR if error.alert else logging.WARNING
            log.log(level, "Cookies not refreshed. Platform=%s Error=%s", platform, error.reason)
        except Exception:
            log.exception("Cookie refresh crashed. Platform=%s", platform)
        else:
            log.info("Cookies refreshed. Platform=%s", platform)


async def refresh_cookies(context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(refresh_cookies_now)


async def run_startup_checks(context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime(context)
    await asyncio.to_thread(run_checks, rt.db if rt else None)


async def heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Proof of life for the container healthcheck: the event loop and scheduler run."""
    rt = runtime(context)
    if rt is not None and rt.service.closing:
        return
    await asyncio.to_thread(health.beat, settings.heartbeat_path)


def schedule(app: Application) -> None:
    jobs = app.job_queue
    if jobs is None:
        log.error("JobQueue is unavailable: no weekly stats, cookie reminders or startup checks.")
        return
    rt = app.bot_data.get("runtime")
    has_stats = isinstance(rt, Runtime) and rt.stats is not None

    jobs.run_repeating(heartbeat, HEARTBEAT_EVERY_SEC, first=0, name="heartbeat")
    jobs.run_daily(maintenance, MAINTENANCE_AT.replace(tzinfo=settings.tz), name="maintenance")
    if has_stats:
        # JobQueue counts days from Sunday, `datetime.weekday()` from Monday.
        weekday = (settings.stats_weekday + 1) % 7
        at = settings.stats_time.replace(tzinfo=settings.tz)
        jobs.run_daily(send_weekly_stats, at, days=(weekday,), name="weekly-stats")
        log.info(
            "Weekly stats scheduled. Weekday=%d Time=%s TZ=%s",
            settings.stats_weekday,
            at.strftime("%H:%M"),
            settings.tz,
        )

    jobs.run_daily(
        check_cookie_expiry, COOKIE_CHECK_AT.replace(tzinfo=settings.tz), name="cookie-expiry"
    )
    if any(settings.check_urls.values()):
        jobs.run_once(run_startup_checks, STARTUP_CHECK_DELAY_SEC, name="startup-checks")
        if hours := settings.cookies_refresh_hours:
            interval = timedelta(hours=hours)
            jobs.run_repeating(refresh_cookies, interval, first=interval, name="cookie-refresh")
            log.info("Cookie refresh scheduled. Every=%dh", hours)
