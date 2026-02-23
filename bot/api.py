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


def find_media_file(song_unique_id: str, artist: str = '', title: str = '') -> dict | None:
    """
    Ищет медиафайл на станции по unique_id.
    Если не найдено — пробует match по artist+title.
    Возвращает dict файла или None.
    """
    try:
        url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/files"
        r = requests.get(url, headers=API_HEADERS, timeout=30)
        if r.status_code != 200:
            return None
        all_files = r.json()

        # 1. Точное совпадение по unique_id
        for f in all_files:
            if f.get('unique_id') == song_unique_id:
                return f

        # 2. Резервный поиск по artist + title
        if artist and title:
            a_low = artist.strip().lower()
            t_low = title.strip().lower()
            for f in all_files:
                if f.get('artist', '').strip().lower() == a_low and \
                   f.get('title', '').strip().lower() == t_low:
                    return f

        return None
    except Exception as e:
        logging.error(f"API Error (find_media_file): {e}")
        return None


def add_media_to_playlist(media_id: int, playlist_id: int) -> tuple[bool, str]:
    """
    Добавляет медиафайл (по числовому id) в плейлист через AzuraCast API.
    Получает текущие данные файла, добавляет playlist_id в список playlists и
    сохраняет обратно через PUT.
    """
    try:
        # Получаем текущие данные файла
        get_url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/file/{media_id}"
        r = requests.get(get_url, headers=API_HEADERS, timeout=10)
        if r.status_code != 200:
            return False, f"GET file error {r.status_code}"

        file_data = r.json()

        # Проверяем, не добавлен ли уже плейлист
        playlists = file_data.get('playlists', [])
        existing_ids = set()
        for p in playlists:
            if isinstance(p, dict):
                existing_ids.add(p.get('id'))
            elif isinstance(p, int):
                existing_ids.add(p)

        if playlist_id in existing_ids:
            return True, "already_in_playlist"

        # Добавляем новый плейлист
        playlists.append({"id": playlist_id})
        file_data['playlists'] = playlists

        # AzuraCast требует float для числовых полей длительности/позиций
        for float_field in ('length', 'amplify', 'fade_overlap', 'fade_in', 'fade_out', 'cue_in', 'cue_out'):
            if float_field in file_data and file_data[float_field] is not None:
                file_data[float_field] = float(file_data[float_field])

        put_url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/file/{media_id}"
        r2 = requests.put(put_url, headers=API_HEADERS, json=file_data, timeout=10)
        if r2.status_code in (200, 201):
            return True, "added"
        return False, f"PUT error {r2.status_code}: {r2.text[:200]}"
    except Exception as e:
        logging.error(f"API Error (add_media_to_playlist): {e}")
        return False, str(e)


def is_media_in_playlist(song_unique_id: str, playlist_id: int) -> bool:
    """Проверяет, находится ли трек (по unique_id) в указанном плейлисте."""
    media = find_media_file(song_unique_id)
    if not media:
        return False
    playlists = media.get('playlists', [])
    for p in playlists:
        if isinstance(p, dict) and p.get('id') == playlist_id:
            return True
        if isinstance(p, int) and p == playlist_id:
            return True
    return False
