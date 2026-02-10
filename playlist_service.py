import os
import tempfile
import math
from datetime import datetime

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
    # Добавьте свои плейлисты сюда
}

def get_readable_playlist_name(raw_name):
    """Преобразует 'default' -> 'Общий эфир'."""
    return PLAYLIST_NAMES.get(raw_name, raw_name)

def format_times(minutes_list):
    """Формирует строку вида 'сейчас, через 3 и 5 минут'."""
    parts = []
    
    # 0 означает, что трек играет прямо сейчас или начнется с секунды на секунду
    if 0 in minutes_list:
        parts.append("прямо сейчас")
    
    # Фильтруем будущее время
    future = [str(m) for m in minutes_list if m > 0]
    
    if future:
        times_str = " и ".join(future) if len(future) < 3 else ", ".join(future)
        parts.append(f"через {times_str} мин")
    
    return ", ".join(parts)

def run(api, tts, queue, intro_text):
    global _intro_was_in_queue

    # 1. Поиск Intro в очереди (для триггера срабатывания)
    intro_in_queue = any(
        (i.get("song", {}).get("text") == intro_text or i.get("song", {}).get("title") == intro_text)
        and not i.get("is_played")
        for i in queue
    )

    # 2. Логика: Интро закончилось (было True, стало False)
    if _intro_was_in_queue and not intro_in_queue:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [PlaylistService] Intro завершилось. Анализ очереди...")
        
        try:
            playlist_timings = {} # Словарь: "Название": [минуты...]
            cumulative_seconds = 0
            
            # Берем первые 10 треков из очереди
            for item in queue[:10]:
                raw_pl = (item.get("playlist") or "default").strip()
                duration = item.get("duration", 0)

                # --- ПРОВЕРКА НА ИГНОР ---
                if raw_pl in IGNORED_PLAYLISTS:
                    # Мы НЕ добавляем этот плейлист в список на озвучку,
                    # НО мы добавляем его длительность к общему времени,
                    # чтобы тайминг следующего трека был верным.
                    cumulative_seconds += duration
                    continue

                # --- ОБРАБОТКА ОБЫЧНОГО ПЛЕЙЛИСТА ---
                pretty_name = get_readable_playlist_name(raw_pl)
                
                # Считаем, через сколько минут начнется (округляем вниз)
                start_minute = math.floor(cumulative_seconds / 60)
                
                if pretty_name not in playlist_timings:
                    playlist_timings[pretty_name] = []
                
                # Добавляем время (избегаем дублей типа "через 3 и 3 минуты")
                if start_minute not in playlist_timings[pretty_name]:
                    playlist_timings[pretty_name].append(start_minute)
                
                # Увеличиваем счетчик времени для следующего трека
                cumulative_seconds += duration

            # --- ГЕНЕРАЦИЯ ТЕКСТА ---
            if not playlist_timings:
                # Если в очереди только игнорируемые плейлисты или пусто
                text = "Далее на радио отличная музыка."
            else:
                phrases = []
                for pl_name, times in playlist_timings.items():
                    time_str = format_times(times)
                    phrases.append(f"{pl_name}: {time_str}")
                
                # Собираем предложение
                text = "Далее в эфире. " + ". ".join(phrases) + "."

            print(f"[PlaylistService] Текст для TTS: {text}")

            # --- СИНТЕЗ ---
            with tempfile.TemporaryDirectory() as td:
                fpath = os.path.join(td, "next5.mp3")
                tts.synth(text, fpath)
                api.upload_file(fpath, "tts_next5.mp3")
                print(f"[PlaylistService] Файл tts_next5.mp3 успешно обновлен.")

        except Exception as e:
            print(f"[PlaylistService] Ошибка: {e}")
            import traceback
            traceback.print_exc()

    _intro_was_in_queue = intro_in_queue