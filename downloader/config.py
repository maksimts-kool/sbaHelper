"""
Конфигурация сервиса загрузки видео.
"""
import os
import re

# --- Telegram ---
DOWNLOADER_BOT_TOKEN = os.getenv("DOWNLOADER_BOT_TOKEN", "")

# --- Ограничения ---
# Telegram позволяет отправлять файлы до 50 МБ через Bot API
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))  # 10 минут

# --- Временная директория для загрузок ---
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/downloader_videos")

# --- Куки для авторизации (Netscape cookies.txt) ---
# Нужны для TikTok-видео с ограниченным доступом.
# Путь к файлу внутри контейнера, например: /app/cookies/tiktok.txt
COOKIES_FILE = os.getenv("COOKIES_FILE", "")

# --- Разрешённые чаты ---
# Список chat_id через запятую, например: "-1001234567890,-1009876543210"
# Если пусто — бот работает везде (не рекомендуется)
_allowed_raw = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS: set[int] = {
    int(cid.strip()) for cid in _allowed_raw.split(",") if cid.strip().lstrip("-").isdigit()
}

# --- Поддерживаемые URL ---
SUPPORTED_URL_PATTERNS: list[re.Pattern] = [
    re.compile(r"https?://vm\.tiktok\.com/\S+", re.IGNORECASE),
    re.compile(r"https?://(www\.)?tiktok\.com/\S+", re.IGNORECASE),
    re.compile(r"https?://(www\.)?youtube\.com/shorts/\S+", re.IGNORECASE),
]
