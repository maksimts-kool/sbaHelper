"""
Обёртки для запросов к AzuraCast API, которые нужны боту.
"""
import logging

import requests

from core.config import API_HEADERS, AZURACAST_HOST, STATION_ID


def get_station_data() -> dict | None:
    """Получает данные о текущем треке (NowPlaying)."""
    try:
        url = f"{AZURACAST_HOST}/api/nowplaying/{STATION_ID}"
        r = requests.get(url, headers=API_HEADERS, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logging.error(f"API Error (NowPlaying): {e}")
        return None


def get_queue_data() -> list:
    """Получает список очереди воспроизведения."""
    try:
        url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/queue"
        r = requests.get(url, headers=API_HEADERS, timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        logging.error(f"API Error (Queue): {e}")
        return []


def skip_song_api() -> tuple[bool, str]:
    """Отправляет команду пропустить текущий трек."""
    try:
        url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/backend/skip"
        r = requests.post(url, headers=API_HEADERS, timeout=10)
        return (True, "Skipped") if r.status_code == 200 else (False, f"Error {r.status_code}")
    except Exception as e:
        return False, str(e)
