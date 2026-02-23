import os
import time
import requests
import tempfile
from datetime import datetime

_uploaded_file_id = None
_file_path_on_server = "tts_time_announce.mp3"
_last_announced_hour = None
_cleanup_timestamp = 0


def get_russian_hour(h):
    if 5 <= h <= 20:
        return f"{h} часов"
    if h % 10 == 1:
        return f"{h} час"
    if h % 10 in [2, 3, 4]:
        return f"{h} часа"
    return f"{h} часов"


def get_now_playing_data(api):
    try:
        url = f"{api.host}/nowplaying/{api.station_id}"
        r = requests.get(url, headers=api.headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        print(f"[TimeService] Ошибка API NowPlaying: Статус {r.status_code}")
    except Exception as e:
        print(f"[TimeService] Ошибка соединения NowPlaying: {e}")
    return None


def run(api, tts, queue, intro_text):
    global _uploaded_file_id, _last_announced_hour, _cleanup_timestamp

    now_ts = time.time()
    current_hour = datetime.now().hour

    # --- ЧИСТКА ---
    if _uploaded_file_id is not None:
        if now_ts > _cleanup_timestamp:
            print("[TimeService] Сброс флага активности (таймер истек).")
            _uploaded_file_id = None
            _cleanup_timestamp = 0

    # --- ПРОВЕРКИ ---
    if _last_announced_hour == current_hour:
        return
    if _uploaded_file_id is not None:
        return
    if not queue:
        return

    np_data = get_now_playing_data(api)
    if not np_data:
        return

    # --- АНАЛИЗ ---
    target_text = intro_text.lower().strip()

    current_track = np_data.get("now_playing", {})
    current_song = current_track.get("song", {})
    current_title = (current_song.get("title") or current_song.get("text") or "").strip()

    current_remaining = current_track.get("remaining")
    if current_remaining is None:
        current_remaining = float(current_track.get("duration", 0)) - float(current_track.get("elapsed", 0))

    found_intro = False
    cleanup_delay = 0

    # СЦЕНАРИЙ А: Intro играет ПРЯМО СЕЙЧАС
    if current_title.lower() == target_text:
        if current_remaining < 5:
            print(f"[TimeService] Intro заканчивается ({int(current_remaining)} сек). Пропуск.")
            return
        found_intro = True
        cleanup_delay = current_remaining + 60
        print("[TimeService] НАШЕЛ! Intro играет сейчас.")

    # СЦЕНАРИЙ Б: Intro СЛЕДУЮЩЕЕ (Queue[0])
    else:
        item = queue[0]
        song = item.get("song", {})
        title = (song.get("title") or song.get("text") or "").strip()
        duration = float(item.get("duration", 0))

        if title.lower() == target_text:
            found_intro = True
            cleanup_delay = current_remaining + duration + 60
            print("[TimeService] НАШЕЛ! Intro следующее в очереди.")

    if not found_intro:
        return

    # --- ДЕЙСТВИЕ ---
    try:
        text = f"Сейчас {get_russian_hour(current_hour)}"
        print(f"[TimeService] Генерирую: '{text}'")

        with tempfile.TemporaryDirectory() as td:
            local = os.path.join(td, "time.mp3")
            tts.synth(text, local, add_silence=True)
            api.upload_file(local, _file_path_on_server)
            api.enqueue_file(_file_path_on_server)

            _uploaded_file_id = "queued_manually"
            _cleanup_timestamp = time.time() + cleanup_delay
            _last_announced_hour = current_hour

            print("[TimeService] УСПЕШНО! Анонс времени добавлен в очередь.")

    except Exception as e:
        print(f"[TimeService] Ошибка выполнения: {e}")
        _uploaded_file_id = None
