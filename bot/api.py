"""
Обёртки для запросов к AzuraCast API, которые нужны боту.
"""
import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.config import API_HEADERS, AZURACAST_HOST, STATION_ID

logger = logging.getLogger(__name__)

_CLIENT: httpx.AsyncClient | None = None
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


async def _get_client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.AsyncClient(headers=API_HEADERS, timeout=_TIMEOUT)
    return _CLIENT


async def close_api_client() -> None:
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
    _CLIENT = None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    reraise=True,
)
async def _request(method: str, url: str, **kwargs) -> httpx.Response:
    client = await _get_client()
    response = await client.request(method, url, **kwargs)
    response.raise_for_status()
    return response


async def get_station_data() -> dict | None:
    """Получает данные о текущем треке (NowPlaying)."""
    url = f"{AZURACAST_HOST}/api/nowplaying/{STATION_ID}"
    try:
        response = await _request("GET", url)
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.warning("API Error (NowPlaying): status=%s", e.response.status_code)
    except httpx.HTTPError as e:
        logger.error("API Error (NowPlaying): %s", e)
    return None


async def get_queue_data() -> list:
    """Получает список очереди воспроизведения."""
    url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/queue"
    try:
        response = await _request("GET", url)
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.warning("API Error (Queue): status=%s", e.response.status_code)
    except httpx.HTTPError as e:
        logger.error("API Error (Queue): %s", e)
    return []


async def skip_song_api() -> tuple[bool, str]:
    """Отправляет команду пропустить текущий трек."""
    url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/backend/skip"
    try:
        await _request("POST", url)
        return True, "Skipped"
    except httpx.HTTPStatusError as e:
        return False, f"Error {e.response.status_code}"
    except httpx.HTTPError as e:
        logger.error("API Error (Skip): %s", e)
        return False, str(e)


async def get_playlist_info(playlist_id: int) -> dict | None:
    """Получает информацию о плейлисте по ID."""
    url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/playlist/{playlist_id}"
    try:
        response = await _request("GET", url)
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.warning("API Error (Playlist Info): id=%s status=%s", playlist_id, e.response.status_code)
    except httpx.HTTPError as e:
        logger.error("API Error (Playlist Info): %s", e)
    return None


async def get_playlist_songs(playlist_id: int) -> list:
    """Получает список треков плейлиста, фильтруя все медиафайлы станции."""
    url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/files"
    try:
        response = await _request("GET", url)
        all_files = response.json()
        songs = []
        for file_entry in all_files:
            playlists = file_entry.get('playlists', [])
            if isinstance(playlists, list):
                for playlist in playlists:
                    if isinstance(playlist, dict) and playlist.get('id') == playlist_id:
                        songs.append(file_entry)
                        break
                    if isinstance(playlist, int) and playlist == playlist_id:
                        songs.append(file_entry)
                        break
        songs.sort(key=lambda x: (x.get('artist', '') + x.get('title', '')).lower())
        return songs
    except httpx.HTTPStatusError as e:
        logger.warning("API Error (Playlist Songs): playlist_id=%s status=%s", playlist_id, e.response.status_code)
    except httpx.HTTPError as e:
        logger.error("API Error (Playlist Songs): %s", e)
    return []


async def find_media_file(song_unique_id: str, artist: str = '', title: str = '') -> dict | None:
    """
    Ищет медиафайл на станции по song_id (32-символьный хеш из NowPlaying song.id).
    Если не найдено — пробует match по artist+title.
    Возвращает dict файла или None.
    """
    url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/files"
    try:
        response = await _request("GET", url)
        all_files = response.json()

        for file_entry in all_files:
            if file_entry.get('song_id') == song_unique_id:
                return file_entry

        if artist and title:
            artist_lower = artist.strip().lower()
            title_lower = title.strip().lower()
            for file_entry in all_files:
                if file_entry.get('artist', '').strip().lower() == artist_lower and \
                   file_entry.get('title', '').strip().lower() == title_lower:
                    return file_entry
    except httpx.HTTPStatusError as e:
        logger.warning("API Error (find_media_file): status=%s", e.response.status_code)
    except httpx.HTTPError as e:
        logger.error("API Error (find_media_file): %s", e)
    return None


async def add_media_to_playlist(media_id: int, playlist_id: int) -> tuple[bool, str]:
    """
    Добавляет медиафайл (по числовому id) в плейлист через AzuraCast API.
    Получает текущие данные файла, добавляет playlist_id в список playlists и
    сохраняет обратно через PUT.
    """
    get_url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/file/{media_id}"
    put_url = get_url
    try:
        response = await _request("GET", get_url)
        file_data = response.json()

        playlists = file_data.get('playlists', [])
        existing_ids = set()
        for playlist in playlists:
            if isinstance(playlist, dict):
                existing_ids.add(playlist.get('id'))
            elif isinstance(playlist, int):
                existing_ids.add(playlist)

        if playlist_id in existing_ids:
            return True, "already_in_playlist"

        playlists.append({"id": playlist_id})
        file_data['playlists'] = playlists

        for float_field in ('length', 'amplify', 'fade_overlap', 'fade_in', 'fade_out', 'cue_in', 'cue_out'):
            if float_field in file_data and file_data[float_field] is not None:
                file_data[float_field] = float(file_data[float_field])

        await _request("PUT", put_url, json=file_data)
        return True, "added"
    except httpx.HTTPStatusError as e:
        logger.error("API Error (add_media_to_playlist): media_id=%s status=%s", media_id, e.response.status_code)
        return False, f"HTTP {e.response.status_code}"
    except httpx.HTTPError as e:
        logger.error("API Error (add_media_to_playlist): %s", e)
        return False, str(e)


async def is_media_in_playlist(song_unique_id: str, playlist_id: int) -> bool:
    """Проверяет, находится ли трек (по song_id хешу из NowPlaying) в указанном плейлисте."""
    media = await find_media_file(song_unique_id)
    if not media:
        return False
    playlists = media.get('playlists', [])
    for playlist in playlists:
        if isinstance(playlist, dict) and playlist.get('id') == playlist_id:
            return True
        if isinstance(playlist, int) and playlist == playlist_id:
            return True
    return False


async def get_schedule(rows: int = 48) -> list:
    """Получает расписание станции (текущие и ближайшие события)."""
    url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/schedule"
    try:
        response = await _request("GET", url, params={"rows": rows})
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.warning("API Error (Schedule): status=%s", e.response.status_code)
    except httpx.HTTPError as e:
        logger.error("API Error (Schedule): %s", e)
    return []


async def get_station_history(limit: int = 5) -> list:
    """
    Возвращает последние `limit` сыгранных треков через API истории.
    Каждый элемент: {'song_id': str, 'display_title': str, 'artist': str, 'title': str}
    """
    url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/history"
    try:
        response = await _request("GET", url)
        history = response.json()
        result = []
        seen = set()
        for entry in history:
            song = entry.get('song', {})
            song_id = song.get('id', '')
            if not song_id or song_id in seen:
                continue
            seen.add(song_id)
            artist = song.get('artist', '')
            title = song.get('title', '')
            text = song.get('text', '')
            from bot.formatters import clean_track_info

            display_title = clean_track_info(artist, title, text)
            result.append({
                'song_id': song_id,
                'display_title': display_title,
                'artist': artist,
                'title': title,
            })
            if len(result) >= limit:
                break
        return result
    except httpx.HTTPStatusError as e:
        logger.warning("API Error (get_station_history): status=%s", e.response.status_code)
    except httpx.HTTPError as e:
        logger.error("API Error (get_station_history): %s", e)
    return []
