"""
Конфигурация сервиса загрузки видео.
"""
import os
import re

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
# Можно указать общий файл COOKIES_FILE или отдельные файлы по платформам.
COOKIES_FILE = os.getenv("COOKIES_FILE", "")
COOKIES_FILE_TIKTOK = os.getenv("COOKIES_FILE_TIKTOK", "").strip()
COOKIES_FILE_FACEBOOK = os.getenv("COOKIES_FILE_FACEBOOK", "").strip()
COOKIES_FILE_YOUTUBE = os.getenv("COOKIES_FILE_YOUTUBE", "").strip()

# --- Куки из браузера ---
# Удобно для локального запуска на той же машине, где уже выполнен вход в Facebook.
# Поддерживаемые браузеры yt-dlp: brave, chrome, chromium, edge, firefox, opera, safari, vivaldi, whale
COOKIES_BROWSER = os.getenv("COOKIES_BROWSER", "").strip().lower()
COOKIES_BROWSER_PROFILE = os.getenv("COOKIES_BROWSER_PROFILE", "").strip()
COOKIES_BROWSER_KEYRING = os.getenv("COOKIES_BROWSER_KEYRING", "").strip().lower()
COOKIES_BROWSER_CONTAINER = os.getenv("COOKIES_BROWSER_CONTAINER", "").strip()

# Иногда сайт требует тот же User-Agent, что и у браузера с актуальной сессией.
DOWNLOADER_USER_AGENT = os.getenv("DOWNLOADER_USER_AGENT", "").strip()

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
    re.compile(r"https?://(?:www\.|m\.|mbasic\.)?facebook\.com/\S+", re.IGNORECASE),
    re.compile(r"https?://fb\.watch/\S+", re.IGNORECASE),
]
