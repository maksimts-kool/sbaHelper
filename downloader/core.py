"""
Ядро загрузки видео.
Использует yt-dlp для скачивания видео с TikTok / YouTube Shorts / Facebook.
"""
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Callable

import yt_dlp

from downloader.config import (
    COOKIES_BROWSER,
    COOKIES_BROWSER_CONTAINER,
    COOKIES_BROWSER_KEYRING,
    COOKIES_BROWSER_PROFILE,
    COOKIES_FILE,
    DOWNLOAD_DIR,
    DOWNLOADER_USER_AGENT,
    MAX_DURATION_SEC,
    MAX_FILE_SIZE_MB,
)

logger = logging.getLogger(__name__)

SUPPORTED_BROWSER_COOKIE_SOURCES = {
    "brave",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "opera",
    "safari",
    "vivaldi",
    "whale",
}


@dataclass
class VideoInfo:
    title: str
    uploader: str
    duration: int       # секунды
    thumbnail: str | None
    view_count: int | None
    like_count: int | None


@dataclass
class DownloadResult:
    file_path: str
    info: VideoInfo


class DownloadError(Exception):
    """Ошибка при загрузке видео."""


class VideoTooLongError(DownloadError):
    """Видео превышает допустимую длительность."""


class FileTooLargeError(DownloadError):
    """Файл превышает допустимый размер для Telegram."""


class UnsupportedContentError(DownloadError):
    """Тип контента не поддерживается (стримы, фото)."""


# --------------------------------------------------------------------------- #
#  Утилиты                                                                     #
# --------------------------------------------------------------------------- #

def _clean_filename(name: str) -> str:
    """Убирает опасные символы из имени файла."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:80]


def _ensure_dir() -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    return DOWNLOAD_DIR


def _get_browser_cookie_spec() -> tuple[str, str | None, str | None, str | None] | None:
    if not COOKIES_BROWSER:
        return None
    if COOKIES_BROWSER not in SUPPORTED_BROWSER_COOKIE_SOURCES:
        logger.warning(
            "Unsupported COOKIES_BROWSER=%r. Ignoring browser cookies.",
            COOKIES_BROWSER,
        )
        return None
    return (
        COOKIES_BROWSER,
        COOKIES_BROWSER_PROFILE or None,
        COOKIES_BROWSER_KEYRING or None,
        COOKIES_BROWSER_CONTAINER or None,
    )


def _apply_auth_options(ydl_opts: dict) -> None:
    auth_sources: list[str] = []

    if COOKIES_FILE:
        if os.path.exists(COOKIES_FILE):
            ydl_opts["cookiefile"] = COOKIES_FILE
            auth_sources.append(f"cookiefile={COOKIES_FILE}")
        else:
            logger.warning("COOKIES_FILE is set but not found: %s", COOKIES_FILE)

    browser_cookie_spec = _get_browser_cookie_spec()
    if browser_cookie_spec:
        ydl_opts["cookiesfrombrowser"] = browser_cookie_spec
        browser_name, profile, _, container = browser_cookie_spec
        details = [browser_name]
        if profile:
            details.append(f"profile={profile}")
        if container:
            details.append(f"container={container}")
        auth_sources.append("browser=" + ", ".join(details))

    if DOWNLOADER_USER_AGENT:
        ydl_opts.setdefault("http_headers", {})["User-Agent"] = DOWNLOADER_USER_AGENT
        auth_sources.append("custom-user-agent")

    if auth_sources:
        logger.debug("yt-dlp auth options enabled: %s", "; ".join(auth_sources))
    else:
        logger.debug("yt-dlp auth options are not configured.")


def _is_facebook_url(url: str) -> bool:
    lowered = url.lower()
    return "facebook.com" in lowered or "fb.watch" in lowered


def _is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in url.lower()


def _is_youtube_shorts_url(url: str) -> bool:
    return "youtube.com/shorts/" in url.lower()


def _get_format_selector(url: str) -> str:
    """
    Возвращает безопасный format selector для разных платформ.

    Идея:
    - YouTube Shorts: предпочитаем раздельные video/audio до 1080p, затем progressive fallback.
    - Facebook Reels: сначала пробуем готовый mp4, затем fallback на video+audio.
    - TikTok: чаще всего лучший результат даёт готовый mp4 без агрессивного выбора дорожек.
    """
    if _is_youtube_shorts_url(url):
        return (
            "bestvideo[ext=mp4][height<=1080][vcodec!=none]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
            "best[ext=mp4][height<=1080]/best[height<=1080]/best"
        )

    if _is_facebook_url(url):
        return (
            "best[ext=mp4][height<=1080]/best[height<=1080]/"
            "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
            "best"
        )

    if _is_tiktok_url(url):
        return "best[ext=mp4][height<=1080]/best[height<=1080]/best"

    return (
        "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
        "best[ext=mp4][height<=1080]/best[height<=1080]/best"
    )


def _parse_compact_count(raw_value: str) -> int | None:
    cleaned = raw_value.strip().replace(",", "").replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMB])?", cleaned, re.IGNORECASE)
    if not match:
        return None

    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    multiplier = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
    }[suffix]
    return int(number * multiplier)


def _pick_first_text(meta: dict, *keys: str) -> str | None:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def _looks_like_generated_tiktok_title(title: str | None) -> bool:
    if not title:
        return False
    return bool(re.fullmatch(r"TikTok video #\d+", title.strip(), re.IGNORECASE))


def _pick_first_int(meta: dict, *keys: str) -> int | None:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            parsed = _parse_compact_count(value)
            if parsed is not None:
                return parsed
    return None


def _normalize_video_info(url: str, meta: dict, duration: int) -> VideoInfo:
    title = _pick_first_text(meta, "title", "fulltitle", "alt_title") or "Без названия"
    uploader = _pick_first_text(
        meta,
        "uploader",
        "channel",
        "creator",
        "artist",
        "channel_follower",
        "uploader_id",
        "channel_id",
    ) or "Неизвестно"
    view_count = _pick_first_int(meta, "view_count", "play_count")
    like_count = _pick_first_int(meta, "like_count", "repost_count")

    if _is_facebook_url(url) and isinstance(title, str):
        parts = [part.strip() for part in title.split("|")]
        if len(parts) >= 3:
            stats_part = parts[0]
            possible_title = " | ".join(part for part in parts[1:-1] if part)
            possible_author = parts[-1]

            view_match = re.search(r"([\d.,KMBkmb]+)\s+views\b", stats_part, re.IGNORECASE)
            reaction_match = re.search(r"([\d.,KMBkmb]+)\s+reactions?\b", stats_part, re.IGNORECASE)

            parsed_view_count = _parse_compact_count(view_match.group(1)) if view_match else None
            parsed_like_count = _parse_compact_count(reaction_match.group(1)) if reaction_match else None

            if possible_title:
                title = possible_title
            if possible_author and uploader == "Неизвестно":
                uploader = possible_author
            if parsed_view_count is not None and (view_count in (None, 0) or parsed_view_count > view_count):
                view_count = parsed_view_count
            if parsed_like_count is not None and like_count in (None, 0):
                like_count = parsed_like_count

    if _is_tiktok_url(url):
        if _looks_like_generated_tiktok_title(title):
            better_title = _pick_first_text(meta, "description", "fulltitle", "alt_title")
            if better_title:
                title = better_title

        better_uploader = _pick_first_text(meta, "channel", "creator", "uploader", "uploader_id")
        if better_uploader:
            uploader = better_uploader

    return VideoInfo(
        title=title,
        uploader=uploader,
        duration=duration,
        thumbnail=meta.get("thumbnail"),
        view_count=view_count,
        like_count=like_count,
    )


def _rewrite_download_error(url: str, err_msg: str) -> str:
    lower_err = err_msg.lower()
    if _is_facebook_url(url) and any(
        token in lower_err
        for token in (
            "login required",
            "not logged in",
            "requires authentication",
            "please log in",
            "content isn't available",
            "video unavailable",
        )
    ):
        return (
            "Facebook запросил авторизацию. Укажите свежий COOKIES_FILE "
            "или настройте COOKIES_BROWSER (например firefox/chrome/edge) "
            "с профилем, где вы уже вошли в аккаунт."
        )
    return err_msg


# --------------------------------------------------------------------------- #
#  Публичный API                                                               #
# --------------------------------------------------------------------------- #

def fetch_info(url: str) -> VideoInfo:
    """
    Получает метаданные видео без загрузки.
    Raises DownloadError при ошибке.
    """
    logger.debug("fetch_info: %s", url)
    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    _apply_auth_options(ydl_opts)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        err_msg = _rewrite_download_error(url, str(e))
        if "unsupported url" in err_msg.lower() and ("/photo/" in url.lower() or "/photo/" in err_msg.lower()):
            raise UnsupportedContentError("Фото-посты не поддерживаются. Только обычные видео.") from e
        raise DownloadError(err_msg) from e

    # Отклоняем прямые трансляции (включая архивные)
    _live_status = meta.get("live_status") or ""
    if meta.get("is_live") or _live_status in ("is_live", "is_upcoming", "was_live", "post_live"):
        raise UnsupportedContentError("Прямые трансляции не поддерживаются. Только обычные видео.")

    # Отклоняем фото-посты (нет видео-дорожки ни в одном формате)
    _formats = meta.get("formats") or []
    if _formats and not any(f.get("vcodec", "none") not in ("none", None) for f in _formats):
        raise UnsupportedContentError("Фото-посты не поддерживаются. Только обычные видео.")

    duration = int(meta.get("duration") or 0)
    if duration > MAX_DURATION_SEC:
        raise VideoTooLongError(
            f"Видео слишком длинное ({duration // 60}:{duration % 60:02d}). "
            f"Максимум — {MAX_DURATION_SEC // 60} мин."
        )

    return _normalize_video_info(url, meta, duration)


def download_video(
    url: str,
    on_progress: Callable[[float], None] | None = None,
) -> DownloadResult:
    """
    Скачивает видео и возвращает путь к файлу.

    :param url: ссылка на видео
    :param on_progress: callback(percent: float) — вызывается во время загрузки
    :raises DownloadError: при любой ошибке yt-dlp
    :raises VideoTooLongError: если видео слоишком длинное
    :raises FileTooLargeError: если файл превышает MAX_FILE_SIZE_MB
    """
    out_dir = _ensure_dir()
    unique_id = uuid.uuid4().hex[:8]
    output_template = os.path.join(out_dir, f"{unique_id}_%(title)s.%(ext)s")

    def _progress_hook(d: dict) -> None:
        if on_progress and d.get("status") == "downloading":
            try:
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                if total:
                    on_progress(downloaded / total * 100)
            except Exception as hook_err:
                # Никогда не ломаем yt-dlp из-за ошибки в коллбэке
                logger.debug("Progress hook error (ignored): %s", hook_err)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": output_template,
        # Для всех платформ сначала пробуем лучший video+audio до 1080p,
        # а если контейнер не mp4, ffmpeg потом приведёт результат к mp4.
        "format": _get_format_selector(url),
        "merge_output_format": "mp4",
        # Не качаем playlist-ы — только одно видео
        "noplaylist": True,
        "progress_hooks": [_progress_hook],
        # Совместимость с TikTok
        "extractor_args": {"tiktok": {"app_version": ""}},
        # Ограничиваем время ожидания сокета
        "socket_timeout": 30,
    }
    _apply_auth_options(ydl_opts)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(_rewrite_download_error(url, str(e))) from e

    duration = int(meta.get("duration") or 0)
    if duration > MAX_DURATION_SEC:
        raise VideoTooLongError(
            f"Видео слишком длинное ({duration // 60}:{duration % 60:02d}). "
            f"Максимум — {MAX_DURATION_SEC // 60} мин."
        )

    # Ищем скачанный файл по шаблону
    downloaded_file: str | None = None
    for fname in os.listdir(out_dir):
        if fname.startswith(unique_id):
            downloaded_file = os.path.join(out_dir, fname)
            break

    if not downloaded_file or not os.path.exists(downloaded_file):
        raise DownloadError("Не удалось найти скачанный файл.")

    size_mb = os.path.getsize(downloaded_file) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        os.remove(downloaded_file)
        raise FileTooLargeError(
            f"Файл слишком большой ({size_mb:.1f} МБ). "
            f"Telegram принимает до {MAX_FILE_SIZE_MB} МБ."
        )

    info = _normalize_video_info(url, meta, duration)

    logger.info("Downloaded: %s (%.1f MB, %ds)", downloaded_file, size_mb, duration)
    return DownloadResult(file_path=downloaded_file, info=info)


def cleanup(file_path: str) -> None:
    """Удаляет временный файл после отправки."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.warning("Could not remove temp file %s: %s", file_path, e)
