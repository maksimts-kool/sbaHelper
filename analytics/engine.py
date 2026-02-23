import json
import os
import time
from datetime import datetime, timedelta

DATA_DIR = "bot_data"
STATS_FILE = os.path.join(DATA_DIR, "stats_daily.json")
HISTORY_FILE = os.path.join(DATA_DIR, "stats_history.json")

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# --- РАБОТА С ФАЙЛАМИ ---

def load_today_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_today_stats(data):
    with open(STATS_FILE, 'w') as f:
        json.dump(data, f)


def get_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            return json.load(f)
    return {}


def log_listener_count(count):
    """Записывает текущее количество слушателей в дневной лог."""
    data = load_today_stats()
    now_str = datetime.now().strftime("%H:%M")
    data.append({"time": now_str, "count": int(count), "ts": time.time()})
    save_today_stats(data)


# --- АНАЛИТИКА ---

def format_duration(seconds):
    """Превращает секунды в читаемый вид."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}ч {mins}м"


def analyze_log(data):
    """
    Главная функция. Считает пик, среднее и интервалы активности.
    Возвращает словарь со статистикой или None если данных нет.
    """
    if not data:
        return None

    counts = [x['count'] for x in data]
    if not counts:
        return None

    max_listeners = max(counts)
    avg_listeners = sum(counts) / len(counts)
    current_listeners = counts[-1]

    intervals = []
    current_session = None

    for entry in data:
        count = entry['count']
        time_str = entry['time']
        ts = entry.get('ts', 0)

        if count > 0:
            if current_session is None:
                current_session = {'start': time_str, 'start_ts': ts, 'max': count}
            else:
                if count > current_session['max']:
                    current_session['max'] = count
        else:
            if current_session is not None:
                current_session['end'] = time_str
                current_session['end_ts'] = ts
                intervals.append(current_session)
                current_session = None

    if current_session is not None:
        current_session['end'] = data[-1]['time']
        current_session['end_ts'] = data[-1]['ts']
        intervals.append(current_session)

    return {
        "max": max_listeners,
        "avg": avg_listeners,
        "current": current_listeners,
        "intervals": intervals,
        "total_checks": len(counts),
    }


def get_today_report_data():
    """Возвращает статистику сегодняшнего дня (без очистки файла)."""
    data = load_today_stats()
    stats = analyze_log(data)
    if not stats:
        return None
    stats['change_percent'] = _calculate_trend(stats['avg'])
    return stats


def rotate_daily_logs():
    """Ночной отчет: сохраняет статистику в историю и очищает дневной файл."""
    data = load_today_stats()
    stats = analyze_log(data)

    if not stats:
        return None

    # Статистика собрана за вчерашний день (задача запускается в полночь)
    yesterday = datetime.now() - timedelta(days=1)
    report_date = yesterday.strftime("%Y-%m-%d")

    # Важно: считаем тренд ДО того, как перезапишем историю
    old_history = get_history()
    stats['change_percent'] = _calculate_trend(stats['avg'], history_data=old_history)
    stats['date'] = report_date

    # Теперь сохраняем вчерашние данные в историю
    old_history["last_day"] = {
        "date": report_date,
        "avg": stats['avg'],
        "max": stats['max'],
    }
    with open(HISTORY_FILE, 'w') as f:
        json.dump(old_history, f)

    # Очищаем дневной файл для нового дня
    with open(STATS_FILE, 'w') as f:
        json.dump([], f)

    return stats


def _calculate_trend(current_avg, history_data=None):
    if not history_data:
        history_data = get_history()
    last_val = history_data.get("last_day", {}).get("avg", 0)
    if last_val == 0:
        return 0
    return ((current_avg - last_val) / last_val) * 100
