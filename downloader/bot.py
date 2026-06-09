"""
Downloader Bot entrypoint.
Listens for TikTok, YouTube, and Facebook links.
"""
import logging
import os

from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from sbahelper.logging import configure_logging
from downloader.service import (
    ALLOWED_CHAT_IDS,
    DOWNLOADER_BOT_TOKEN,
    MAX_FILE_SIZE_MB,
    MAX_SHORT_DURATION_SEC,
    capture_exception,
    init_error_tracking,
    is_transient_network_error,
)
from downloader.handlers import handle_message


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    configure_logging(level_name, quiet_loggers=("yt_dlp", "httpx", "httpcore"))


async def _cmd_start(update, context) -> None:
    await update.message.reply_text(
        "Привет! Я скачиваю <b>короткие</b> видео из <b>TikTok</b>, "
        "<b>YouTube Shorts</b> и <b>Facebook</b>.\n\n"
        "Просто отправь мне ссылку на видео с любого из этих сайтов:\n"
        "- <code>https://www.tiktok.com/...</code>\n"
        "- <code>https://youtube.com/shorts/...</code>\n"
        "- <code>https://www.facebook.com/reel/...</code>\n"
        "- <code>https://fb.watch/...</code>\n\n"
        "Я проверю ссылку и скачаю видео, если это короткий вертикальный ролик "
        f"(до {MAX_SHORT_DURATION_SEC // 60} мин). Горизонтальные и длинные видео, "
        "фото- и обычные посты не поддерживаются.\n\n"
        f"Размер файла - до {MAX_FILE_SIZE_MB} МБ.",
        parse_mode="HTML",
    )


async def _post_init(app) -> None:
    await app.bot.set_my_commands([BotCommand("start", "Информация о боте")])
    me = await app.bot.get_me()
    logging.getLogger(__name__).info("Downloader bot started: @%s (id=%s)", me.username, me.id)
    if ALLOWED_CHAT_IDS:
        logging.getLogger(__name__).info("Allowed chats: %s", ALLOWED_CHAT_IDS)
    else:
        logging.getLogger(__name__).warning("ALLOWED_CHAT_IDS is empty; bot responds everywhere.")


async def _error_handler(update: object, context) -> None:
    if context.error is not None and is_transient_network_error(context.error):
        logging.getLogger(__name__).warning("Transient Telegram network error: %s", context.error)
        return

    exc_info = None
    if context.error is not None:
        exc_info = (type(context.error), context.error, context.error.__traceback__)
        capture_exception(context.error)
    logging.getLogger(__name__).error("Unhandled downloader bot error", exc_info=exc_info)


def main() -> int:
    _configure_logging()
    init_error_tracking("downloader-bot")
    logger = logging.getLogger(__name__)

    if not DOWNLOADER_BOT_TOKEN:
        logger.critical("DOWNLOADER_BOT_TOKEN is not set. Exiting.")
        return 1

    polling_timeout = 30
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=60.0,
        write_timeout=60.0,
        connect_timeout=20.0,
        pool_timeout=20.0,
    )
    polling_request = HTTPXRequest(
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
        .get_updates_request(polling_request)
        .post_init(_post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", _cmd_start))
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    application.add_error_handler(_error_handler)
    application.run_polling(
        timeout=polling_timeout,
        bootstrap_retries=-1,
        drop_pending_updates=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
