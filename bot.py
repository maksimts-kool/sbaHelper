"""
Telegram Bot — entrypoint.
Запускает бота с периодическим обновлением плеера и ежедневными отчетами.
"""
import logging
from datetime import time

import pytz
from telegram import BotCommand
from telegram import Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.ext import ContextTypes
from telegram.request import HTTPXRequest

from bot.api import close_api_client, get_station_history
from bot.handlers import announcement_command, button_callback, changelog_dm_handler, start, votes_command
from bot.jobs import (
    daily_report_job,
    farewell_notice_job,
    radio_decommission_job,
    schedule_notify_job,
    update_display_job,
)
from bot.state import CHATS_DB, add_recent_song, is_radio_decommissioned
from core.config import TELEGRAM_TOKEN, TZ_NAME
from monitoring.logging_utils import configure_logging
from monitoring.runtime import HeartbeatMonitor
from monitoring.sentry import init_sentry
from monitoring.telegram_errors import is_transient_telegram_error

configure_logging("sbaradio-bot")
init_sentry("sbaradio-bot")
logger = logging.getLogger(__name__)
monitor = HeartbeatMonitor("sbaradio-bot")


if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        exit(1)

    if is_radio_decommissioned():
        logger.info("Radio bot is decommissioned. Downloader bot remains the only active bot.")
        exit(0)

    BOT_COMMANDS = [
        BotCommand("start",        "📻 Открыть радио-плеер"),
        BotCommand("votes",        "🗳 Голоса — view / create / edit"),
        BotCommand("announcement", "📢 Анонс плейлиста"),
    ]

    async def post_init(app):
        monitor.beat(status="starting", active_chats=len(CHATS_DB))
        await app.bot.set_my_commands(BOT_COMMANDS)
        me = await app.bot.get_me()
        logger.info("=== Bot started ===")
        logger.info("Bot: @%s (id=%s)", me.username, me.id)
        history = await get_station_history(5)
        for entry in reversed(history):  # oldest first so newest ends up at index 0
            add_recent_song(
                entry['song_id'],
                display_title=entry['display_title'],
                artist=entry['artist'],
                title=entry['title'],
            )
        monitor.beat(status="running", active_chats=len(CHATS_DB), bot_username=me.username)

    async def post_shutdown(app):
        if is_radio_decommissioned():
            try:
                monitor.file_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to remove decommissioned radio monitor file", exc_info=True)
        else:
            monitor.stop(active_chats=len(CHATS_DB))
        await close_api_client()

    async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
        monitor.beat(status="running", active_chats=len(CHATS_DB))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if is_transient_telegram_error(context.error):
            logger.warning("Transient Telegram transport error: %s", context.error)
            monitor.beat(status="running", active_chats=len(CHATS_DB))
            return

        exc_info = None
        if context.error is not None:
            exc_info = (type(context.error), context.error, context.error.__traceback__)
        logger.error("Unhandled telegram bot error", exc_info=exc_info)
        monitor.fail("telegram error", error=str(context.error), active_chats=len(CHATS_DB))

    # Keep regular Bot API calls and long-poll `get_updates()` on separate HTTPX
    # request objects. This avoids relying on `run_polling(..., read_timeout=...)`
    # arguments, which are not available in all PTB versions used by deployments.
    polling_timeout = 30
    request = HTTPXRequest(
        connection_pool_size=10,
        read_timeout=20.0,
        write_timeout=20.0,
        connect_timeout=20.0,
        pool_timeout=20.0,
    )
    polling_request = HTTPXRequest(
        connection_pool_size=10,
        read_timeout=float(polling_timeout + 15),
        write_timeout=20.0,
        connect_timeout=20.0,
        pool_timeout=20.0,
    )

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .request(request)
        .get_updates_request(polling_request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("announcement", announcement_command))
    application.add_handler(CommandHandler("votes", votes_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, changelog_dm_handler))
    application.add_error_handler(error_handler)

    jq = application.job_queue
    jq.run_repeating(
        update_display_job,
        interval=30,
        first=10,
        name="update_display",
        job_kwargs={'misfire_grace_time': 20, 'max_instances': 3},
    )

    midnight = time(hour=0, minute=0, second=0, tzinfo=pytz.timezone(TZ_NAME))
    jq.run_daily(
        daily_report_job,
        time=midnight,
        name="daily_report",
        job_kwargs={'misfire_grace_time': 60},
    )
    jq.run_daily(
        schedule_notify_job,
        time=midnight,
        name="schedule_notify_daily",
        job_kwargs={'misfire_grace_time': 60},
    )
    jq.run_repeating(
        schedule_notify_job,
        interval=60,
        first=15,
        name="schedule_notify_refresh",
        job_kwargs={'misfire_grace_time': 30, 'max_instances': 1},
    )
    jq.run_repeating(
        farewell_notice_job,
        interval=600,
        first=20,
        name="farewell_notice",
        job_kwargs={'misfire_grace_time': 60, 'max_instances': 1},
    )
    jq.run_repeating(
        radio_decommission_job,
        interval=60,
        first=30,
        name="radio_decommission",
        job_kwargs={'misfire_grace_time': 60, 'max_instances': 1},
    )

    jq.run_repeating(
        heartbeat_job,
        interval=60,
        first=5,
        name="heartbeat",
        job_kwargs={'misfire_grace_time': 30, 'max_instances': 1},
    )

    print("Bot started (Optimized for timeouts)...")
    application.run_polling(
        timeout=polling_timeout,
        bootstrap_retries=-1,
    )
