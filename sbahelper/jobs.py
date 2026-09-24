"""Scheduled jobs: the weekly summary, the cookie expiry reminder, startup checks."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, time, timedelta

from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from sbahelper import cookies, texts
from sbahelper.checks import run_checks
from sbahelper.config import settings
from sbahelper.handlers import STATS_KEY, is_allowed, stats_store
from sbahelper.stats import week_period

log = logging.getLogger(__name__)

# Remind admins this long before login cookies expire.
COOKIE_WARNING = timedelta(days=3)
COOKIE_CHECK_AT = time(12)
STARTUP_CHECK_DELAY_SEC = 5


async def send_weekly_stats(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Each active chat gets its own summary; chats without downloads get nothing."""
    store = stats_store(context)
    if store is None:
        return
    start, end = week_period(datetime.now(settings.tz))
    try:
        chat_ids = await asyncio.to_thread(store.active_chat_ids, start, end)
    except sqlite3.Error as error:
        log.error("Weekly stats not read. Error=%s", error)
        return

    for chat_id in filter(is_allowed, chat_ids):
        stats = await asyncio.to_thread(store.weekly_stats, chat_id=chat_id, start=start, end=end)
        try:
            await context.bot.send_message(chat_id, texts.weekly_stats(stats))
        except TelegramError as error:
            log.warning("Weekly stats not sent. Chat=%s Error=%s", chat_id, error)
        else:
            log.info("Weekly stats sent. Chat=%s Downloads=%d", chat_id, stats.total_downloads)

    if settings.stats_retention_days:
        cutoff = end - timedelta(days=settings.stats_retention_days)
        try:
            removed = await asyncio.to_thread(store.prune, older_than=cutoff)
        except sqlite3.Error as error:
            log.warning("Old stats not pruned. Error=%s", error)
        else:
            if removed:
                log.info("Old stats pruned. Rows=%d Before=%s", removed, cutoff.date())


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


async def run_startup_checks(context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(run_checks)


def schedule(app: Application) -> None:
    jobs = app.job_queue
    if jobs is None:
        log.error("JobQueue is unavailable: no weekly stats, cookie reminders or startup checks.")
        return

    if app.bot_data.get(STATS_KEY) is not None:
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
