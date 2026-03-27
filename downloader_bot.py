"""
Downloader Bot — entrypoint.
Слушает сообщения с TikTok / YouTube Shorts ссылками и скачивает видео.
"""
import logging

from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from downloader.config import DOWNLOADER_BOT_TOKEN
from downloader.handlers import handle_message
from monitoring.logging_utils import configure_logging
from monitoring.runtime import HeartbeatMonitor
from monitoring.sentry import init_sentry

configure_logging("downloader-bot")
init_sentry("downloader-bot")
logger = logging.getLogger(__name__)
monitor = HeartbeatMonitor("downloader-bot")

# yt-dlp и httpx слишком шумят на DEBUG, оставляем им WARNING
logging.getLogger("yt_dlp").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

if __name__ == "__main__":
    if not DOWNLOADER_BOT_TOKEN:
        logging.critical("DOWNLOADER_BOT_TOKEN is not set. Exiting.")
        exit(1)

    BOT_COMMANDS = [
        BotCommand("start", "ℹ️ Информация о боте"),
    ]

    async def post_init(app):
        monitor.beat(status="starting")
        await app.bot.set_my_commands(BOT_COMMANDS)
        me = await app.bot.get_me()
        logger.info("=== Downloader bot started ===")
        logger.info("Bot: @%s (id=%s)", me.username, me.id)
        from downloader.config import ALLOWED_CHAT_IDS, MAX_DURATION_SEC, MAX_FILE_SIZE_MB
        if ALLOWED_CHAT_IDS:
            logger.info("Allowed chats: %s", ALLOWED_CHAT_IDS)
        else:
            logger.warning("ALLOWED_CHAT_IDS is empty — bot responds everywhere!")
        logger.info("Limits: max %d MB, max %d sec", MAX_FILE_SIZE_MB, MAX_DURATION_SEC)
        monitor.beat(status="running", bot_username=me.username, allowed_chats=len(ALLOWED_CHAT_IDS))

    async def post_shutdown(app):
        monitor.stop()

    async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
        from downloader.config import ALLOWED_CHAT_IDS

        monitor.beat(status="running", allowed_chats=len(ALLOWED_CHAT_IDS))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        exc_info = None
        if context.error is not None:
            exc_info = (type(context.error), context.error, context.error.__traceback__)
        logger.error("Unhandled downloader bot error", exc_info=exc_info)
        monitor.fail("telegram error", error=str(context.error))

    # `run_polling()` overrides request-level `get_updates()` timeouts unless they
    # are passed explicitly. Keep polling settings aligned to avoid spurious
    # `httpx.ReadError` during normal long-poll waits or brief network glitches.
    polling_timeout = 30
    request = HTTPXRequest(
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
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # /start — краткая инструкция
    async def cmd_start(update, context):
        await update.message.reply_text(
            "👋 Привет! Я скачиваю видео из <b>TikTok</b> и <b>YouTube Shorts</b>.\n\n"
            "Просто отправь мне ссылку:\n"
            "• <code>https://vm.tiktok.com/...</code>\n"
            "• <code>https://www.tiktok.com/...</code>\n"
            "• <code>https://youtube.com/shorts/...</code>\n\n"
            "Максимальная длительность — 5 мин, размер файла — до 50 МБ.",
            parse_mode="HTML",
        )

    application.add_handler(CommandHandler("start", cmd_start))

    # Обработчик всех текстовых сообщений и подписей к медиа
    application.add_handler(
        MessageHandler(filters.TEXT | filters.CAPTION, handle_message)
    )
    application.add_error_handler(error_handler)

    jq = application.job_queue
    jq.run_repeating(
        heartbeat_job,
        interval=60,
        first=5,
        job_kwargs={'misfire_grace_time': 30, 'max_instances': 1},
    )

    application.run_polling(
        timeout=polling_timeout,
        read_timeout=float(polling_timeout + 15),
        write_timeout=60.0,
        connect_timeout=20.0,
        pool_timeout=20.0,
        bootstrap_retries=-1,
        drop_pending_updates=True,
    )
