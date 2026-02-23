"""
Telegram Bot — entrypoint.
Запускает бота с периодическим обновлением плеера и ежедневными отчетами.
"""
import logging
from datetime import time

import pytz
from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler
from telegram.request import HTTPXRequest

from bot.handlers import announcement_command, button_callback, start, view_command
from bot.jobs import daily_report_job, update_display_job
from core.config import TELEGRAM_TOKEN, TZ_NAME

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)


if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        exit(1)

    BOT_COMMANDS = [
        BotCommand("start",        "📻 Открыть радио-плеер (только для админов)"),
        BotCommand("view",         "📊 Просмотр статистики и голосований"),
        BotCommand("announcement", "📢 Анонс плейлиста (только для админов)"),
    ]

    async def post_init(app):
        await app.bot.set_my_commands(BOT_COMMANDS)

    request = HTTPXRequest(
        connection_pool_size=10,
        read_timeout=20.0,
        write_timeout=20.0,
        connect_timeout=20.0,
    )

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).request(request).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("announcement", announcement_command))
    application.add_handler(CommandHandler("view", view_command))
    application.add_handler(CallbackQueryHandler(button_callback))

    jq = application.job_queue
    jq.run_repeating(
        update_display_job,
        interval=30,
        first=10,
        job_kwargs={'misfire_grace_time': 20, 'max_instances': 3},
    )

    midnight = time(hour=0, minute=0, second=0, tzinfo=pytz.timezone(TZ_NAME))
    jq.run_daily(daily_report_job, time=midnight, job_kwargs={'misfire_grace_time': 60})

    print("Bot started (Optimized for timeouts)...")
    application.run_polling()