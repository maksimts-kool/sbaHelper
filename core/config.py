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

# --- Ссылки ---
STREAM_URL = "https://radio.maksimtsikvasvili24.thkit.ee/listen/sbaradio/radio.mp3"
REQUEST_URL = "https://radio.maksimtsikvasvili24.thkit.ee/public/sbaradio/embed-requests"

# --- Фильтрация очереди ---
IGNORED_KEYWORDS = ["intro", "tts next5", "tts time announce", "tts_next5", "tts_time"]

# --- Поднятие песен (Best Playlist) ---
BEST_PLAYLIST_ID = int(os.getenv("BEST_PLAYLIST_ID", "17"))
UPVOTE_THRESHOLD = int(os.getenv("UPVOTE_THRESHOLD", "10"))

# --- Пути к файлам ---
CHATS_FILE = os.path.join("bot_data", "active_chats.json")
UPVOTES_FILE = os.path.join("bot_data", "upvotes.json")
