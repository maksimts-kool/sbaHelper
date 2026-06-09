"""
Ядро загрузки видео.
Использует yt-dlp для скачивания видео с TikTok / YouTube Shorts / Facebook.
"""
import json
import logging
import os
import subprocess
import time
import uuid
from typing import Callable

import yt_dlp

from downloader.metadata import normalize_video_info, pick_video_dimensions
from downloader.models import DownloadResult, VideoInfo
from downloader.platforms import is_facebook_url, is_tiktok_url
from downloader.service import (
    DOWNLOAD_DIR,
    MAX_DURATION_SEC,
    MAX_FILE_SIZE_MB,
    MAX_SHORT_DURATION_SEC,
    exceeds_short_limit,
    is_vertical_video,
)
from downloader.ytdlp_options import (
    YTDLP_RETRY_ATTEMPTS,
    YTDLP_RETRY_MAX_SLEEP_SEC,
    build_ydl_opts,
)

logger = logging.getLogger(__name__)

_DNS_ERROR_TOKENS = (
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname provided, or not known",
    "failed to resolve",
    "getaddrinfo failed",
)
_RETRYABLE_YTDLP_ERROR_TOKENS = _DNS_ERROR_TOKENS + (
    "transporterror",
    "network is unreachable",
    "connection reset by peer",
    "connection aborted",
    "connection refused",
    "remote end closed connection without response",
    "server disconnected",
    "timed out",
    "timeout",
)

class DownloadError(Exception):
    """Ошибка при загрузке видео."""


class VideoTooLongError(DownloadError):
    """Видео превышает допустимую длительность."""


class FileTooLargeError(DownloadError):
    """Файл превышает допустимый размер для Telegram."""


class UnsupportedContentError(DownloadError):
    """Тип контента не поддерживается (стримы, фото)."""


def _ensure_dir() -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    return DOWNLOAD_DIR


def _file_has_audio_stream(file_path: str) -> bool | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                file_path,
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning("Could not inspect audio streams with ffprobe for %s: %s", file_path, e)
        return None

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        logger.warning("Could not parse ffprobe output for %s: %s", file_path, e)
        return None

    return any(stream.get("codec_type") == "audio" for stream in data.get("streams") or [])


def _rewrite_download_error(url: str, err_msg: str) -> str:
    lower_err = err_msg.lower()
    if any(token in lower_err for token in _DNS_ERROR_TOKENS):
        return (
            "Не удалось обратиться к сайту из-за DNS/сетевой ошибки. "
            "Бот уже сделал несколько автоматических попыток, но адрес всё ещё не резолвится. "
            "Проверьте DNS и доступ в интернет у контейнера/сервера."
        )
    if is_facebook_url(url) and any(
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
            "в формате Netscape cookies.txt."
        )
    return err_msg


def _is_retryable_ytdlp_error(err_msg: str) -> bool:
    lower_err = err_msg.lower()
    return any(token in lower_err for token in _RETRYABLE_YTDLP_ERROR_TOKENS)


def _extract_with_retries(url: str, ydl_opts: dict, *, download: bool) -> dict:
    action = "download" if download else "metadata fetch"

    for attempt in range(1, YTDLP_RETRY_ATTEMPTS + 1):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=download)
        except yt_dlp.utils.DownloadError as e:
            if attempt >= YTDLP_RETRY_ATTEMPTS:
                raise

            err_msg = str(e)
            if not _is_retryable_ytdlp_error(err_msg):
                raise

            sleep_for = min(2 ** (attempt - 1), YTDLP_RETRY_MAX_SLEEP_SEC)
            logger.warning(
                "Transient yt-dlp %s error for %s (attempt %d/%d): %s. Retrying in %.1fs",
                action,
                url,
                attempt,
                YTDLP_RETRY_ATTEMPTS,
                err_msg,
                sleep_for,
            )
            time.sleep(sleep_for)

    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- #
#  Публичный API                                                               #
# --------------------------------------------------------------------------- #

def fetch_info(url: str) -> VideoInfo:
    """
    Получает метаданные видео без загрузки.
    Raises DownloadError при ошибке.
    """
    logger.debug("fetch_info: %s", url)
    ydl_opts = build_ydl_opts(url, download=False)
    try:
        meta = _extract_with_retries(url, ydl_opts, download=False)
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

    # Принимаем только вертикальные короткие видео (shorts/reels). Ориентацию
    # проверяем раньше длительности, чтобы обычное горизонтальное видео получало
    # понятную причину отказа, а не «слишком длинное».
    width, height = pick_video_dimensions(meta)
    if not is_vertical_video(width, height):
        raise UnsupportedContentError(
            "Это не вертикальное видео (shorts/reels). "
            "Я скачиваю только короткие вертикальные видео."
        )

    if exceeds_short_limit(duration):
        raise VideoTooLongError(
            f"Видео слишком длинное ({duration // 60}:{duration % 60:02d}). "
            f"Максимум — {MAX_SHORT_DURATION_SEC // 60} мин."
        )

    return normalize_video_info(url, meta, duration)


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

    ydl_opts = build_ydl_opts(
        url,
        download=True,
        output_template=output_template,
        progress_hook=_progress_hook,
    )

    try:
        meta = _extract_with_retries(url, ydl_opts, download=True)
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

    has_audio = _file_has_audio_stream(downloaded_file)
    if is_tiktok_url(url) and has_audio is False:
        os.remove(downloaded_file)
        raise DownloadError(
            "Не удалось скачать TikTok со звуком: полученный файл не содержит аудиодорожку."
        )

    size_mb = os.path.getsize(downloaded_file) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        os.remove(downloaded_file)
        raise FileTooLargeError(
            f"Файл слишком большой ({size_mb:.1f} МБ). "
            f"Telegram принимает до {MAX_FILE_SIZE_MB} МБ."
        )

    info = normalize_video_info(url, meta, duration)

    logger.info("Downloaded: %s (%.1f MB, %ds)", downloaded_file, size_mb, duration)
    return DownloadResult(file_path=downloaded_file, info=info)


def cleanup(file_path: str) -> None:
    """Удаляет временный файл после отправки."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.warning("Could not remove temp file %s: %s", file_path, e)
