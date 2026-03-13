import os

# --- AzuraCast ---
AZURACAST_HOST = os.getenv("AZURACAST_HOST", "https://radio.maksimtsikvasvili24.thkit.ee")
STATION_ID = int(os.getenv("STATION_ID", "2"))
API_KEY = os.getenv("API_KEY", "")
API_HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# --- Telegram Bot ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TZ_NAME = os.getenv("TZ", "Europe/Tallinn")

# --- TTS Worker ---
TTS_VOICE = os.getenv("TTS_VOICE", "ru-RU-SvetlanaNeural")
INTRO_FILE_TEXT = os.getenv("INTRO_FILE_TEXT", "intro")

# --- Schedule TTS ---
SCHEDULE_PLAYLIST_ID = int(os.getenv("SCHEDULE_PLAYLIST_ID", "18"))
SCHEDULE_INTERVAL_MIN = int(os.getenv("SCHEDULE_INTERVAL_MIN", "30"))

# --- Background sounds for schedule TTS ---
BG_START_PATH = os.getenv("BG_START_PATH", os.path.join("assets", "bg_start.mp3"))
BG_MID_PATH = os.getenv("BG_MID_PATH", os.path.join("assets", "bg_mid.mp3"))
BG_END_PATH = os.getenv("BG_END_PATH", os.path.join("assets", "bg_end.mp3"))
BG_FADE_MS = int(os.getenv("BG_FADE_MS", "2500"))

# --- Ссылки ---
STREAM_URL = "https://radio.maksimtsikvasvili24.thkit.ee/listen/sbaradio/radio.mp3"
REQUEST_URL = "https://radio.maksimtsikvasvili24.thkit.ee/public/sbaradio/embed-requests"

# --- Фильтрация очереди ---
IGNORED_KEYWORDS = ["intro","tts_schedule", "schedule"]

# --- Поднятие песен (Best Playlist) ---
BEST_PLAYLIST_ID = int(os.getenv("BEST_PLAYLIST_ID", "17"))
UPVOTE_THRESHOLD = int(os.getenv("UPVOTE_THRESHOLD", "10"))

# --- Пути к файлам ---
CHATS_FILE = os.path.join("bot_data", "active_chats.json")
UPVOTES_FILE = os.path.join("bot_data", "upvotes.json")

# --- Администраторы ---
# Список user_id через запятую, например: "123456789,987654321"
_admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {
    int(uid.strip()) for uid in _admin_ids_raw.split(",") if uid.strip().isdigit()
}
