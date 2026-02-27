"""
Ядро загрузки видео.
Использует yt-dlp для скачивания видео с TikTok / YouTube Shorts.
"""
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Callable

import yt_dlp

from downloader.config import COOKIES_FILE, DOWNLOAD_DIR, MAX_DURATION_SEC, MAX_FILE_SIZE_MB

logger = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    title: str
    uploader: str
    duration: int       # секунды
    thumbnail: str | None


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
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
        logger.debug("fetch_info: using cookies from %s", COOKIES_FILE)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e)
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

    return VideoInfo(
        title=meta.get("title", "Без названия"),
        uploader=meta.get("uploader") or meta.get("channel") or "Неизвестно",
        duration=duration,
        thumbnail=meta.get("thumbnail"),
    )


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
        # Сначала пробуем объединить видео+аудио до 1080p,
        # иначе берём лучший готовый mp4, иначе любой лучший формат
        "format": (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080]+bestaudio/"
            "best[height<=1080][ext=mp4]/"
            "best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        # Не качаем playlist-ы — только одно видео
        "noplaylist": True,
        "progress_hooks": [_progress_hook],
        # Совместимость с TikTok
        "extractor_args": {"tiktok": {"app_version": ""}},
        # Ограничиваем время ожидания сокета
        "socket_timeout": 30,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
        logger.debug("download_video: using cookies from %s", COOKIES_FILE)
    else:
        logger.debug("download_video: no cookies file (COOKIES_FILE=%r)", COOKIES_FILE)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(str(e)) from e

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

    info = VideoInfo(
        title=meta.get("title", "Без названия"),
        uploader=meta.get("uploader") or meta.get("channel") or "Неизвестно",
        duration=duration,
        thumbnail=meta.get("thumbnail"),
    )

    logger.info("Downloaded: %s (%.1f MB, %ds)", downloaded_file, size_mb, duration)
    return DownloadResult(file_path=downloaded_file, info=info)


def cleanup(file_path: str) -> None:
    """Удаляет временный файл после отправки."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.warning("Could not remove temp file %s: %s", file_path, e)
