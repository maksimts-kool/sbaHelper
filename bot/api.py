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


def get_playlist_info(playlist_id: int) -> dict | None:
    """Получает информацию о плейлисте по ID."""
    try:
        url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/playlist/{playlist_id}"
        r = requests.get(url, headers=API_HEADERS, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logging.error(f"API Error (Playlist Info): {e}")
        return None


def get_playlist_songs(playlist_id: int) -> list:
    """Получает список треков плейлиста, фильтруя все медиафайлы станции."""
    try:
        url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/files"
        r = requests.get(url, headers=API_HEADERS, timeout=30)
        if r.status_code != 200:
            return []
        all_files = r.json()
        songs = []
        for f in all_files:
            playlists = f.get('playlists', [])
            if isinstance(playlists, list):
                for pl in playlists:
                    if isinstance(pl, dict) and pl.get('id') == playlist_id:
                        songs.append(f)
                        break
                    elif isinstance(pl, int) and pl == playlist_id:
                        songs.append(f)
                        break
        # Sort by artist+title for consistent ordering
        songs.sort(key=lambda x: (x.get('artist', '') + x.get('title', '')).lower())
        return songs
    except Exception as e:
        logging.error(f"API Error (Playlist Songs): {e}")
        return []
