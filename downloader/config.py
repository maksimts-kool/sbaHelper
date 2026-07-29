"""
Downloader configuration and service wiring.

This is the small "front desk" for the downloader bot: environment settings,
platform/URL detection, Sentry, and smoke checks.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import time

from dotenv import load_dotenv
from telegram.error import NetworkError

from shared import (
    DEFAULT_TRANSIENT_ERROR_TEXT,
    SentryTracker,
    is_transient_error,
)
from shared import (
    configure_logging as configure_shared_logging,
)
from shared import (
    print_results as print_startup_results,
)

load_dotenv()
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

_FALSEY_ENV_VALUES = {"0", "false", "no", "off"}


def env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY_ENV_VALUES


def env_int(name: str, default: int) -> int:
    """Читает целое из окружения, не роняя бота на опечатке в стеке."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer, using %s instead.", name, raw, default)
        return default


def env_clock_time(name: str, default: time) -> time:
    """Читает время суток в формате `ЧЧ:ММ`."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default

    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour=hour, minute=minute)

    logger.warning("%s=%r is not a HH:MM time, using %s instead.", name, raw, default)
    return default


DOWNLOADER_BOT_TOKEN = os.getenv("DOWNLOADER_BOT_TOKEN", "")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))
# Максимальная длительность «короткого» видео (shorts/reels). Видео длиннее
# отклоняется на этапе проверки ссылки. По умолчанию 5 минут.
MAX_SHORT_DURATION_SEC = int(os.getenv("MAX_SHORT_DURATION_SEC", "300"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/downloader_videos")

COOKIES_FILE = os.getenv("COOKIES_FILE", "")
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()

CHECK_YOUTUBE_URL = os.getenv("CHECK_YOUTUBE_URL", "").strip()
CHECK_TIKTOK_URL = os.getenv("CHECK_TIKTOK_URL", "").strip()
STARTUP_CHECKS_REQUIRED = env_flag("STARTUP_CHECKS_REQUIRED", default=True)

# Недельная статистика загрузок. День недели задаётся как в `datetime.weekday()`:
# 0 — понедельник, 6 — воскресенье.
STATS_ENABLED = env_flag("STATS_ENABLED", default=True)
STATS_DB_PATH = os.getenv("STATS_DB_PATH", "/data/downloader_stats.db").strip()
STATS_TIMEZONE = os.getenv("STATS_TIMEZONE", "").strip() or "Europe/Tallinn"
STATS_WEEKLY_WEEKDAY = min(6, max(0, env_int("STATS_WEEKLY_WEEKDAY", 6)))
STATS_WEEKLY_AT = env_clock_time("STATS_WEEKLY_TIME", time(hour=20))
STATS_RETENTION_DAYS = max(0, env_int("STATS_RETENTION_DAYS", 400))

SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "production").strip()
SENTRY_RELEASE = os.getenv("SENTRY_RELEASE", "").strip()

_allowed_raw = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS: set[int] = {
    int(cid.strip()) for cid in _allowed_raw.split(",") if cid.strip().lstrip("-").isdigit()
}


# --------------------------------------------------------------------------- #
# Platform detection
# --------------------------------------------------------------------------- #


def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in url.lower()


def is_youtube_url(url: str) -> bool:
    lowered = url.lower()
    return "youtube.com/" in lowered or "youtu.be/" in lowered


def detect_platform(url: str) -> str:
    """Площадка ссылки — ключ разбивки в недельной статистике."""
    if is_tiktok_url(url):
        return "tiktok"
    if is_youtube_url(url):
        return "youtube"
    return "other"


# --------------------------------------------------------------------------- #
# Supported links
# --------------------------------------------------------------------------- #

# Принимаем любую ссылку на основной домен платформы (включая поддомены вроде
# www / m / vm / vt / web). Что именно делать со ссылкой — решает уже проверка
# метаданных: это должно быть короткое видео (shorts/reel), а не длинное видео,
# фото-пост или обычный пост. См. `exceeds_short_limit` и `fetch_info`.
SUPPORTED_URL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"https?://(?:[\w-]+\.)?tiktok\.com/\S+", re.IGNORECASE),
    re.compile(r"https?://(?:[\w-]+\.)?youtube\.com/\S+", re.IGNORECASE),
    re.compile(r"https?://youtu\.be/\S+", re.IGNORECASE),
]


def extract_supported_url(text: str) -> str | None:
    for pattern in SUPPORTED_URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).rstrip(".,);")
    return None


# URL-формы, которые НЕ являются отдельным видео: профили, каналы, подборки,
# разделы. Их игнорируем, чтобы случайно не начать скачивать чужой профиль.
_NON_VIDEO_URL_PATTERNS: list[re.Pattern[str]] = [
    # TikTok: профиль (@user без /video/ или /photo/) и разделы-подборки.
    re.compile(r"https?://(?:[\w-]+\.)?tiktok\.com/@[^/?#\s]+/?(?:[?#]|$)", re.IGNORECASE),
    re.compile(
        r"https?://(?:[\w-]+\.)?tiktok\.com/(?:tag|music|discover|foryou|following|live|search|explore)\b",
        re.IGNORECASE,
    ),
    # YouTube: каналы, плейлисты, ленты, поиск, лента shorts (без id).
    re.compile(
        r"https?://(?:[\w-]+\.)?youtube\.com/"
        r"(?:@[^/?#\s]+|channel/|c/|user/|playlist\b|feed/|results\b|hashtag/|shorts/?(?:[?#]|$))",
        re.IGNORECASE,
    ),
]


def is_non_video_url(url: str) -> bool:
    """True для ссылок на профиль/канал/подборку (а не на отдельное видео)."""
    return any(pattern.search(url) for pattern in _NON_VIDEO_URL_PATTERNS)


def exceeds_short_limit(duration: int) -> bool:
    """True, если видео длиннее допустимой длины «короткого» ролика.

    Применяется ко всем поддерживаемым платформам (YouTube, TikTok):
    бот скачивает только короткие видео (shorts/reels), а не полноразмерные.
    Длительность 0 (неизвестна) считается допустимой и не отклоняется здесь.
    """
    return duration > MAX_SHORT_DURATION_SEC


def is_vertical_video(width: int | None, height: int | None) -> bool:
    """True, если видео вертикальное (портрет) — признак shorts/reels.

    Бот принимает только вертикальные короткие видео, а обычные (горизонтальные)
    ролики отклоняет. Если размеры неизвестны, видео считается подходящим и не
    отклоняется здесь.
    """
    if not width or not height:
        return True
    return height > width


# --------------------------------------------------------------------------- #
# Error tracking
# --------------------------------------------------------------------------- #

_TRANSIENT_ERROR_TEXT = DEFAULT_TRANSIENT_ERROR_TEXT + ("http client says",)


def is_transient_network_error(error: BaseException) -> bool:
    return is_transient_error(
        error,
        exception_types=(NetworkError,),
        text_markers=_TRANSIENT_ERROR_TEXT,
    )


_sentry_tracker = SentryTracker(
    dsn_getter=lambda: SENTRY_DSN,
    environment_getter=lambda: SENTRY_ENVIRONMENT,
    release_getter=lambda: SENTRY_RELEASE,
    is_transient=is_transient_network_error,
    text_markers=_TRANSIENT_ERROR_TEXT,
)


def init_error_tracking(service_name: str) -> bool:
    return _sentry_tracker.init(service_name)


def capture_exception(error: BaseException) -> None:
    _sentry_tracker.capture_exception(error)


def flush_error_tracking(timeout: float = 2.0) -> None:
    _sentry_tracker.flush(timeout=timeout)


# --------------------------------------------------------------------------- #
# Smoke checks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LinkCheck:
    service: str
    url: str


@dataclass(frozen=True)
class LinkCheckResult:
    service: str
    ok: bool
    message: str
    info: object | None = None
    blocks_startup: bool = True


def configured_checks() -> list[LinkCheck]:
    return [
        LinkCheck("youtube", CHECK_YOUTUBE_URL),
        LinkCheck("tiktok", CHECK_TIKTOK_URL),
    ]


def run_check(check: LinkCheck) -> LinkCheckResult:
    from downloader.download import DownloadError, UnsupportedContentError, fetch_info

    if not check.url:
        # Пустой CHECK_* URL — это не ошибка конфигурации, а «проверка не
        # настроена»: пропускаем её, не блокируя запуск.
        return LinkCheckResult(check.service, True, "not configured", blocks_startup=False)

    supported_url = extract_supported_url(check.url)
    if not supported_url:
        return LinkCheckResult(check.service, False, "unsupported url format")

    try:
        info = fetch_info(supported_url)
    except UnsupportedContentError as error:
        return LinkCheckResult(check.service, False, str(error), blocks_startup=False)
    except DownloadError as error:
        blocks_startup = not is_nonblocking_check_error(check, error)
        if blocks_startup:
            capture_exception(error)
        return LinkCheckResult(
            check.service,
            False,
            str(error),
            blocks_startup=blocks_startup,
        )
    except Exception as error:
        capture_exception(error)
        logger.exception("Unexpected check error for %s", check.service)
        return LinkCheckResult(check.service, False, f"unexpected error: {error}")

    return LinkCheckResult(
        check.service,
        True,
        f"{info.title} by {info.uploader} ({info.duration}s)",
        info,
        blocks_startup=False,
    )


def run_checks() -> list[LinkCheckResult]:
    return [run_check(check) for check in configured_checks()]


def is_nonblocking_check_error(check: LinkCheck, error: BaseException) -> bool:
    if is_transient_network_error(error):
        return True

    message = str(error).lower()
    return check.service == "tiktok" and ("http error 403" in message or "forbidden" in message)


def print_results(results: list[LinkCheckResult]) -> None:
    print_startup_results(results)


def result_exit_code(results: list[LinkCheckResult]) -> int:
    if any(not result.ok and result.blocks_startup for result in results):
        return 1
    return 0


def configure_logging() -> None:
    configure_shared_logging(
        "INFO",
        quiet_loggers=("yt_dlp", "httpx", "httpcore"),
    )
