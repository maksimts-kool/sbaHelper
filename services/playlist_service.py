import os
import tempfile
import math
from datetime import datetime

from core.config import IGNORED_KEYWORDS

_intro_was_in_queue = False

# --- НАСТРОЙКА НАЗВАНИЙ ПЛЕЙЛИСТОВ ---
IGNORED_PLAYLISTS = [
    "intro",
]

# Формат: "техническое_имя": "Произносимое имя"
PLAYLIST_NAMES = {
    "default": "Общие",
    "russian": "Русские",
    "estonian": "Эстонские",
    "requested": "Запросы",
    "90s": "90-е",
    "jazz": "Джаз",
    "radio 2025": "Радио 2025-го",
    "bass": "Динамичные",
    "bi 2": "Би 2",
    # Добавьте свои плейлисты сюда
}


def get_readable_playlist_name(raw_name):
    """Преобразует техническое название плейлиста в читаемое."""
    return PLAYLIST_NAMES.get(raw_name, raw_name)


def format_times(minutes_list):
    """Формирует строку вида 'прямо сейчас, через 3 и 5 минут'."""
    parts = []
    if 0 in minutes_list:
        parts.append("прямо сейчас")
    future = [str(m) for m in minutes_list if m > 0]
    if future:
        times_str = " и ".join(future) if len(future) < 3 else ", ".join(future)
        parts.append(f"через {times_str} мин")
    return ", ".join(parts)


def run(api, tts, queue, intro_text):
    global _intro_was_in_queue

    # 1. Поиск Intro в очереди
    intro_in_queue = any(
        (i.get("song", {}).get("text") == intro_text or i.get("song", {}).get("title") == intro_text)
        and not i.get("is_played")
        for i in queue
    )

    # 2. Логика: Интро закончилось (было True, стало False) — генерируем анонс
    if _intro_was_in_queue and not intro_in_queue:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [PlaylistService] Intro завершилось. Анализ очереди...")

        try:
            playlist_timings = {}
            cumulative_seconds = 0
            right_now_claimed = False  # «прямо сейчас» может быть только у первого плейлиста

            for item in queue[:10]:
                raw_pl = (item.get("playlist") or "default").strip()
                duration = item.get("duration", 0)

                # Пропускаем служебные треки по названию (intro, tts time announce и т.д.)
                song = item.get("song", {})
                song_text = (song.get("text") or song.get("title") or "").lower()
                if any(kw in song_text for kw in IGNORED_KEYWORDS):
                    cumulative_seconds += duration
                    continue

                if raw_pl in IGNORED_PLAYLISTS:
                    cumulative_seconds += duration
                    continue

                pretty_name = get_readable_playlist_name(raw_pl)
                start_minute = math.floor(cumulative_seconds / 60)

                # Если этот плейлист встречается впервые и расчёт даёт 0 минут —
                # разрешаем «прямо сейчас» только однажды.
                if pretty_name not in playlist_timings:
                    if start_minute == 0:
                        if not right_now_claimed:
                            right_now_claimed = True
                        else:
                            start_minute = 1  # сдвигаем: «через 1 мин» вместо второго «прямо сейчас»
                    playlist_timings[pretty_name] = []

                if start_minute not in playlist_timings[pretty_name]:
                    playlist_timings[pretty_name].append(start_minute)

                cumulative_seconds += duration

            if not playlist_timings:
                text = "Далее на радио отличная музыка."
            else:
                phrases = []
                for pl_name, times in playlist_timings.items():
                    time_str = format_times(times)
                    phrases.append(f"{pl_name}: {time_str}")
                text = "Далее в эфире. " + ". ".join(phrases) + "."

            print(f"[PlaylistService] Текст для TTS: {text}")

            with tempfile.TemporaryDirectory() as td:
                fpath = os.path.join(td, "next5.mp3")
                tts.synth(text, fpath)
                api.upload_file(fpath, "tts_next5.mp3")
                print("[PlaylistService] Файл tts_next5.mp3 успешно обновлен.")

        except Exception as e:
            print(f"[PlaylistService] Ошибка: {e}")
            import traceback
            traceback.print_exc()

    _intro_was_in_queue = intro_in_queue
