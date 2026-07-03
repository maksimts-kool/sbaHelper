"""
Ядро загрузки видео.
Использует yt-dlp для скачивания видео с TikTok / YouTube Shorts / Facebook.

Здесь собраны модели, опции yt-dlp, нормализация метаданных и сам движок загрузки.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import yt_dlp

from downloader.config import (
    COOKIES_FILE,
    DOWNLOAD_DIR,
    MAX_DURATION_SEC,
    MAX_FILE_SIZE_MB,
    MAX_SHORT_DURATION_SEC,
    YOUTUBE_COOKIES_FILE,
    exceeds_short_limit,
    is_facebook_url,
    is_tiktok_url,
    is_vertical_video,
    is_youtube_url,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Модели                                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class VideoInfo:
    title: str
    uploader: str
    duration: int
    thumbnail: str | None
    view_count: int | None
    like_count: int | None
    width: int | None = None
    height: int | None = None


@dataclass
class DownloadResult:
    file_path: str
    info: VideoInfo


# --------------------------------------------------------------------------- #
#  Опции yt-dlp                                                                #
# --------------------------------------------------------------------------- #

YTDLP_RETRY_ATTEMPTS = 3
YTDLP_RETRY_MAX_SLEEP_SEC = 8.0


def get_cookie_file_for_url(url: str) -> str:
    if is_youtube_url(url):
        return YOUTUBE_COOKIES_FILE
    return COOKIES_FILE.strip()


def apply_auth_options(ydl_opts: dict, url: str) -> None:
    cookie_file = get_cookie_file_for_url(url)
    if cookie_file:
        if os.path.exists(cookie_file):
            ydl_opts["cookiefile"] = cookie_file
            logger.debug("yt-dlp cookie file enabled: %s", cookie_file)
            if is_youtube_url(url):
                ydl_opts["js_runtimes"] = {"node": {}}
                ydl_opts["remote_components"] = ["ejs:github"]
        else:
            logger.warning("Cookie file is set but not found: %s", cookie_file)
    else:
        logger.debug("yt-dlp cookie file is not configured for this URL.")


def build_ydl_opts(
    url: str,
    *,
    download: bool,
    output_template: str | None = None,
    progress_hook: Callable[[dict], None] | None = None,
) -> dict:
    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": YTDLP_RETRY_ATTEMPTS,
        "extractor_retries": YTDLP_RETRY_ATTEMPTS,
        "file_access_retries": YTDLP_RETRY_ATTEMPTS,
    }

    if is_tiktok_url(url):
        ydl_opts["extractor_args"] = {"tiktok": {"app_version": ""}}

    if download:
        if not output_template:
            raise ValueError("output_template is required when download=True")
        ydl_opts.update(
            {
                "outtmpl": output_template,
                "format": get_format_selector(url),
                "merge_output_format": "mp4",
            }
        )
        if progress_hook is not None:
            ydl_opts["progress_hooks"] = [progress_hook]
    else:
        ydl_opts["skip_download"] = True
        ydl_opts["format"] = get_metadata_format_selector(url)
        # Не разворачиваем канал/плейлист целиком (иначе профиль перечисляется
        # десятками секунд). Отдельное видео извлекается полностью как обычно.
        ydl_opts["extract_flat"] = "in_playlist"

    apply_auth_options(ydl_opts, url)
    return ydl_opts


def get_metadata_format_selector(url: str) -> str:
    if is_tiktok_url(url):
        return "best[ext=mp4][height<=1080]/best[height<=1080]/best"

    return (
        "best*[vcodec!=none][height<=1080]/"
        "bestvideo*[vcodec!=none][height<=1080]/"
        "best*[vcodec!=none]/"
        "bestvideo*[vcodec!=none]/"
        "best"
    )


def get_format_selector(url: str) -> str:
    if is_youtube_url(url):
        return (
            "bestvideo[ext=mp4][height<=1080][vcodec!=none]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
            "best[ext=mp4][height<=1080]/best[height<=1080]/best"
        )

    if is_facebook_url(url):
        return (
            "best[ext=mp4][height<=1080]/best[height<=1080]/"
            "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
            "best"
        )

    if is_tiktok_url(url):
        size_limit = f"{MAX_FILE_SIZE_MB}M"
        return (
            f"best[ext=mp4][height<=1080][vcodec=h264][acodec!=none][filesize<{size_limit}]/"
            f"best[ext=mp4][height<=1080][vcodec=h264][acodec!=none][filesize_approx<{size_limit}]/"
            "best[ext=mp4][height<=1080][vcodec=h264][acodec!=none]/"
            "best[height<=1080][vcodec=h264][acodec!=none]/"
            "bestvideo[ext=mp4][height<=1080][vcodec!=none]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
            "best[ext=mp4][height<=1080][vcodec!=none][acodec!=none]/"
            "best[height<=1080][vcodec!=none][acodec!=none]/"
            "best[ext=mp4][height<=1080]/best[height<=1080]/best"
        )

    return (
        "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
        "best[ext=mp4][height<=1080]/best[height<=1080]/best"
    )


# --------------------------------------------------------------------------- #
#  Нормализация метаданных                                                     #
# --------------------------------------------------------------------------- #


def parse_compact_count(raw_value: str) -> int | None:
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


def pick_first_text(meta: dict, *keys: str) -> str | None:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def looks_like_generated_tiktok_title(title: str | None) -> bool:
    if not title:
        return False
    return bool(re.fullmatch(r"TikTok video #\d+", title.strip(), re.IGNORECASE))


def pick_video_dimensions(meta: dict) -> tuple[int | None, int | None]:
    """Возвращает (ширина, высота) видео для определения ориентации.

    Сначала берём размеры верхнего уровня, иначе — из первого видео-формата
    (у всех форматов одного видео одинаковая ориентация).
    """
    width = pick_first_int(meta, "width")
    height = pick_first_int(meta, "height")
    if width and height:
        return width, height

    for fmt in meta.get("formats") or []:
        fmt_width = fmt.get("width")
        fmt_height = fmt.get("height")
        if fmt_width and fmt_height:
            return int(fmt_width), int(fmt_height)

    return width, height


def pick_first_int(meta: dict, *keys: str) -> int | None:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            parsed = parse_compact_count(value)
            if parsed is not None:
                return parsed
    return None


def normalize_video_info(url: str, meta: dict, duration: int) -> VideoInfo:
    title = pick_first_text(meta, "title", "fulltitle", "alt_title") or "Без названия"
    uploader = (
        pick_first_text(
            meta,
            "uploader",
            "channel",
            "creator",
            "artist",
            "channel_follower",
            "uploader_id",
            "channel_id",
        )
        or "Неизвестно"
    )
    view_count = pick_first_int(meta, "view_count", "play_count")
    like_count = pick_first_int(meta, "like_count", "repost_count")

    if is_facebook_url(url) and isinstance(title, str):
        parts = [part.strip() for part in title.split("|")]
        if len(parts) >= 3:
            stats_part = parts[0]
            possible_title = " | ".join(part for part in parts[1:-1] if part)
            possible_author = parts[-1]

            view_match = re.search(r"([\d.,KMBkmb]+)\s+views\b", stats_part, re.IGNORECASE)
            reaction_match = re.search(r"([\d.,KMBkmb]+)\s+reactions?\b", stats_part, re.IGNORECASE)

            parsed_view_count = parse_compact_count(view_match.group(1)) if view_match else None
            parsed_like_count = (
                parse_compact_count(reaction_match.group(1)) if reaction_match else None
            )

            if possible_title:
                title = possible_title
            if possible_author and uploader == "Неизвестно":
                uploader = possible_author
            if parsed_view_count is not None and (
                view_count is None or view_count == 0 or parsed_view_count > view_count
            ):
                view_count = parsed_view_count
            if parsed_like_count is not None and like_count in (None, 0):
                like_count = parsed_like_count

    if is_tiktok_url(url):
        if looks_like_generated_tiktok_title(title):
            better_title = pick_first_text(meta, "description", "fulltitle", "alt_title")
            if better_title:
                title = better_title

        better_uploader = pick_first_text(meta, "channel", "creator", "uploader", "uploader_id")
        if better_uploader:
            uploader = better_uploader

    width, height = pick_video_dimensions(meta)

    return VideoInfo(
        title=title,
        uploader=uploader,
        duration=duration,
        thumbnail=meta.get("thumbnail"),
        view_count=view_count,
        like_count=like_count,
        width=width,
        height=height,
    )


# --------------------------------------------------------------------------- #
#  Движок загрузки                                                             #
# --------------------------------------------------------------------------- #

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
        if "unsupported url" in err_msg.lower() and (
            "/photo/" in url.lower() or "/photo/" in err_msg.lower()
        ):
            raise UnsupportedContentError("Здесь только фото, без видео — скачивать нечего.") from e
        raise DownloadError(err_msg) from e

    # Профиль / канал / подборка — это не отдельное видео.
    if meta.get("_type") == "playlist" or meta.get("entries") is not None:
        raise UnsupportedContentError(
            "Это ссылка на профиль или подборку, а не на отдельное видео."
        )

    # Отклоняем прямые трансляции (включая архивные)
    _live_status = meta.get("live_status") or ""
    if meta.get("is_live") or _live_status in ("is_live", "is_upcoming", "was_live", "post_live"):
        raise UnsupportedContentError(
            "Это прямая трансляция — её скачать не получится, только обычные видео."
        )

    # Отклоняем фото-посты (нет видео-дорожки ни в одном формате)
    _formats = meta.get("formats") or []
    if _formats and not any(f.get("vcodec", "none") not in ("none", None) for f in _formats):
        raise UnsupportedContentError("Здесь только фото, без видео — скачивать нечего.")

    duration = int(meta.get("duration") or 0)

    # Принимаем только вертикальные короткие видео (shorts/reels). Ориентацию
    # проверяем раньше длительности, чтобы обычное горизонтальное видео получало
    # понятную причину отказа, а не «слишком длинное».
    width, height = pick_video_dimensions(meta)
    if not is_vertical_video(width, height):
        raise UnsupportedContentError(
            "Я умею скачивать только короткие вертикальные видео (shorts / reels), "
            "это видео не подойдёт."
        )

    if exceeds_short_limit(duration):
        raise VideoTooLongError(
            f"Я умею скачивать только короткие видео — до {MAX_SHORT_DURATION_SEC // 60} мин, "
            f"а это длиннее ({duration // 60}:{duration % 60:02d})."
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
            f"Я умею скачивать только короткие видео — до {MAX_DURATION_SEC // 60} мин, "
            f"а это длиннее ({duration // 60}:{duration % 60:02d})."
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
            f"Файл слишком большой ({size_mb:.1f} МБ). Telegram принимает до {MAX_FILE_SIZE_MB} МБ."
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
