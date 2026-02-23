"""
Функции форматирования текста и клавиатуры для сообщений бота.
"""
import time as time_module
from datetime import datetime

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from analytics import engine as analytics_engine
from bot.state import VOTE_STATE, get_skip_progress
from core.config import IGNORED_KEYWORDS, REQUEST_URL, STREAM_URL, TZ_NAME


# --- УТИЛИТЫ ---

def escape_md(text: str) -> str:
    """Экранирует спецсимволы для обычного Markdown (не V2)."""
    if not text:
        return ""
    for char in ['_', '*', '`', '[', ']']:
        text = text.replace(char, f"\\{char}")
    return text


def clean_track_info(artist: str, title: str, raw_text: str) -> str:
    """Убирает дублирование, например: 'Artist - Song - Song' -> 'Artist - Song'."""
    if artist and title:
        if title == artist:
            return title
        final_str = f"{artist} - {title}"
    else:
        final_str = raw_text

    if " - " in final_str:
        parts = final_str.split(" - ")
        if len(parts) >= 2 and parts[-1] == parts[-2]:
            parts.pop()
            final_str = " - ".join(parts)

    return final_str


def format_duration(seconds) -> str:
    if not seconds:
        return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02}:{s:02}"


def create_progress_bar(elapsed, total, length=10) -> str:
    """Генерирует строку вида: `01:20 ━━🔘━━━━━━ 03:45`"""
    if total == 0:
        return ""
    percent = min(elapsed / total, 1.0)
    filled_length = int(length * percent)
    bar = '━' * filled_length + '🔘' + '─' * (length - filled_length)
    return f"`{format_duration(elapsed)} {bar} {format_duration(total)}`"


# --- КЛАВИАТУРА ---

def get_keyboard(listeners: int) -> InlineKeyboardMarkup:
    votes, required = get_skip_progress(listeners)
    btn_listen = InlineKeyboardButton("🎧 Слушать", url=STREAM_URL)
    btn_skip = InlineKeyboardButton(f"⏭ Пропустить ({votes}/{required})", callback_data="vote_skip")
    btn_request = InlineKeyboardButton("📝 Заказать трек", url=REQUEST_URL)
    return InlineKeyboardMarkup([[btn_listen], [btn_skip, btn_request]])


# --- ФОРМАТИРОВАНИЕ СООБЩЕНИЙ ---

def format_main_message(data: dict) -> tuple[str, str, int, str]:
    """Сообщение 1: Сейчас играет. Возвращает (текст, url_обложки, слушатели, song_id)."""
    np = data['now_playing']
    song = np['song']
    listeners = data['listeners']['total']
    song_id = song.get('id', song.get('text'))

    elapsed = np.get('elapsed', 0)
    duration = np.get('duration', 0)
    playlist_raw = np.get('playlist', 'General')
    playlist = escape_md(playlist_raw)

    is_request = np.get('is_request') or str(playlist_raw).lower() == 'requested'
    req_mark = "🎷 **Заказ!** " if is_request else ""

    raw_title = song.get('text', 'Unknown')
    raw_artist = song.get('artist', '')
    raw_track = song.get('title', '')
    full_title_md = escape_md(clean_track_info(raw_artist, raw_track, raw_title))

    art_url = song.get('art', '')
    progress_bar = create_progress_bar(elapsed, duration)
    tz = pytz.timezone(TZ_NAME)

    text = (
        f"📻 **SBA Radio Live**\n\n"
        f"🎶 **Сейчас играет:**\n{req_mark}{full_title_md}\n"
        f"{progress_bar}\n\n"
        f"📂 **Плейлист:** {playlist}\n"
        f"👥 **Слушают:** {listeners}\n"
        f"🕒 **Обновлено:** {datetime.now(tz).strftime('%H:%M:%S')}"
    )
    return text, art_url, listeners, song_id


def format_queue_list(queue_data: list) -> str:
    """Сообщение 2: Далее в эфире."""
    if not queue_data or not isinstance(queue_data, list):
        return "📂 **Очередь воспроизведения пуста.**"

    lines = []
    count = 0
    current_ts = time_module.time()

    for item in queue_data:
        if count >= 5:
            break

        song = item.get('song', {})
        if isinstance(song, str):
            raw_text, raw_artist, raw_title = song.strip(), "", ""
        else:
            raw_text = song.get('text', '').strip()
            raw_artist = song.get('artist', '').strip()
            raw_title = song.get('title', '').strip()

        if any(ignored in raw_text.lower() for ignored in IGNORED_KEYWORDS):
            continue

        clean_text = clean_track_info(raw_artist, raw_title, raw_text)
        text_md = escape_md(clean_text)

        playlist_raw = item.get('playlist', '')
        playlist_md = escape_md(playlist_raw)

        played_at = item.get('played_at', 0)
        duration = item.get('duration', 0)
        is_request = item.get('is_request', False) or str(playlist_raw).lower() == 'requested'

        starts_in_min = 0
        if played_at > 0:
            starts_in_sec = played_at - current_ts
            if starts_in_sec > 0:
                starts_in_min = int(starts_in_sec // 60)

        infos = []
        if played_at > 0:
            infos.append(f"⏳ {starts_in_min} мин")
        if duration > 0:
            infos.append(f"⏱ {format_duration(duration)}")
        if playlist_md:
            infos.append(f"📂 {playlist_md}")

        info_str = " | ".join(infos)
        req_icon = "🎷 " if is_request else ""
        lines.append(f"{count + 1}. {req_icon}**{text_md}**\n   {info_str}")
        count += 1

    if not lines:
        return "📂 **Далее в эфире:**\n_(Только служебные джинглы)_"

    header = "🔜 **Далее в эфире:**\n━━━━━━━━━━━━━━━━━━\n"
    return header + "\n".join(lines)


def format_intervals_text(intervals: list) -> str:
    if not intervals:
        return "\n😴 Активности не было."
    text = "\n⏱ **Активность по времени:**\n"
    for idx, i in enumerate(intervals, 1):
        dur = i['end_ts'] - i['start_ts']
        text += (
            f"{idx}️⃣ `{i['start']} — {i['end']}` "
            f"({analytics_engine.format_duration(dur)}) | 👥 {i['max']}\n"
        )
    return text
