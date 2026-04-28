"""
Конфигурация сервиса загрузки видео.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
DOWNLOADER_BOT_TOKEN = os.getenv("DOWNLOADER_BOT_TOKEN", "")

# --- Ограничения ---
# Telegram позволяет отправлять файлы до 50 МБ через Bot API
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))  # 10 минут

# --- Временная директория для загрузок ---
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/downloader_videos")

# --- Куки для авторизации (Netscape cookies.txt) ---
# Нужны для видео с ограниченным доступом (TikTok / Facebook и т.д.).
# Путь к файлу внутри контейнера, например: /app/cookies/auth.txt
COOKIES_FILE = os.getenv("COOKIES_FILE", "")
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()

# --- Проверка поддерживаемых сервисов ---
CHECK_YOUTUBE_URL = os.getenv("CHECK_YOUTUBE_URL", "").strip()
CHECK_TIKTOK_URL = os.getenv("CHECK_TIKTOK_URL", "").strip()
CHECK_FACEBOOK_URL = os.getenv("CHECK_FACEBOOK_URL", "").strip()
STARTUP_CHECKS_REQUIRED = os.getenv("STARTUP_CHECKS_REQUIRED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# --- Error tracking ---
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "production").strip()
SENTRY_RELEASE = os.getenv("SENTRY_RELEASE", "").strip()

# --- Разрешённые чаты ---
# Список chat_id через запятую, например: "-1001234567890,-1009876543210"
# Если пусто — бот работает везде (не рекомендуется)
_allowed_raw = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS: set[int] = {
    int(cid.strip()) for cid in _allowed_raw.split(",") if cid.strip().lstrip("-").isdigit()
}
