import json
import os
import time
from datetime import datetime

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
        except:
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
    """Записывает данные."""
    data = load_today_stats()
    now_str = datetime.now().strftime("%H:%M")
    # ts нужен для точного расчета длительности
    data.append({"time": now_str, "count": int(count), "ts": time.time()})
    save_today_stats(data)

# --- НОВАЯ УНИВЕРСАЛЬНАЯ ЛОГИКА (ОСТАЛЬНОЕ УДАЛЕНО) ---

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
    Главная функция. Считает ВСЁ: пик, среднее и интервалы.
    Возвращает словарь со статистикой.
    """
    if not data:
        return None

    counts = [x['count'] for x in data]
    if not counts:
        return None

    # 1. Базовая статистика
    max_listeners = max(counts)
    avg_listeners = sum(counts) / len(counts)
    current_listeners = counts[-1]

    # 2. Поиск интервалов (Сессий)
    intervals = []
    current_session = None

    for entry in data:
        count = entry['count']
        time_str = entry['time']
        ts = entry.get('ts', 0)

        if count > 0:
            if current_session is None:
                # Старт сессии
                current_session = {
                    'start': time_str, 
                    'start_ts': ts,
                    'max': count
                }
            else:
                # Обновляем пик внутри сессии
                if count > current_session['max']:
                    current_session['max'] = count
        else:
            # Если слушателей 0, но сессия была активна -> закрываем её
            if current_session is not None:
                current_session['end'] = time_str
                current_session['end_ts'] = ts
                intervals.append(current_session)
                current_session = None

    # Если лог закончился, а кто-то еще слушает -> закрываем сессию текущим временем
    if current_session is not None:
        current_session['end'] = data[-1]['time']
        current_session['end_ts'] = data[-1]['ts']
        intervals.append(current_session)

    return {
        "max": max_listeners,
        "avg": avg_listeners,
        "current": current_listeners,
        "intervals": intervals,
        "total_checks": len(counts)
    }

def get_today_report_data():
    """Для команды /stats (без очистки)."""
    data = load_today_stats()
    stats = analyze_log(data)
    if not stats: return None
    
    # Добавляем процент изменения
    stats['change_percent'] = _calculate_trend(stats['avg'])
    return stats

def rotate_daily_logs():
    """Для ночного отчета (с очисткой)."""
    data = load_today_stats()
    stats = analyze_log(data)
    
    if not stats:
        return None

    # Сохраняем в историю вчерашний день
    history = get_history()
    history["last_day"] = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "avg": stats['avg'],
        "max": stats['max']
    }
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f)

    # Очищаем файл
    with open(STATS_FILE, 'w') as f:
        json.dump([], f)
    
    stats['change_percent'] = _calculate_trend(stats['avg'], history_data=history)
    stats['date'] = history["last_day"]["date"]
    return stats

def _calculate_trend(current_avg, history_data=None):
    if not history_data:
        history_data = get_history()
    
    last_val = history_data.get("last_day", {}).get("avg", 0)
    if last_val == 0:
        return 0
    return ((current_avg - last_val) / last_val) * 100