"""
Downloader Bot — entrypoint.
Слушает сообщения с TikTok / YouTube Shorts ссылками и скачивает видео.
"""
import logging

from telegram import BotCommand
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from telegram.request import HTTPXRequest

from downloader.config import DOWNLOADER_BOT_TOKEN
from downloader.handlers import handle_message

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
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
        await app.bot.set_my_commands(BOT_COMMANDS)
        me = await app.bot.get_me()
        logging.info("=== Downloader bot started ===")
        logging.info("Bot: @%s (id=%s)", me.username, me.id)
        from downloader.config import ALLOWED_CHAT_IDS, MAX_DURATION_SEC, MAX_FILE_SIZE_MB
        if ALLOWED_CHAT_IDS:
            logging.info("Allowed chats: %s", ALLOWED_CHAT_IDS)
        else:
            logging.warning("ALLOWED_CHAT_IDS is empty — bot responds everywhere!")
        logging.info("Limits: max %d MB, max %d sec", MAX_FILE_SIZE_MB, MAX_DURATION_SEC)

    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=60.0,   # видео могут грузиться долго
        write_timeout=60.0,
        connect_timeout=20.0,
    )

    application = (
        ApplicationBuilder()
        .token(DOWNLOADER_BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    # /start — краткая инструкция
    from telegram.ext import CommandHandler

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

    application.run_polling(drop_pending_updates=True)
