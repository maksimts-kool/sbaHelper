"""
Downloader service wiring.

This is the small "front desk" for the downloader bot: environment settings,
supported URL detection, Sentry, smoke checks, and container startup.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv
from telegram.error import NetworkError

from sbahelper.errors import DEFAULT_TRANSIENT_ERROR_TEXT, SentryTracker, is_transient_error
from sbahelper.logging import configure_logging as configure_shared_logging
from sbahelper.startup import print_results as print_startup_results


load_dotenv()
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

DOWNLOADER_BOT_TOKEN = os.getenv("DOWNLOADER_BOT_TOKEN", "")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))
# Максимальная длительность «короткого» видео (shorts/reels). Видео длиннее
# отклоняется на этапе проверки ссылки. По умолчанию 3 минуты — максимум для
# YouTube Shorts.
MAX_SHORT_DURATION_SEC = int(os.getenv("MAX_SHORT_DURATION_SEC", "180"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/downloader_videos")

COOKIES_FILE = os.getenv("COOKIES_FILE", "")
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()

CHECK_YOUTUBE_URL = os.getenv("CHECK_YOUTUBE_URL", "").strip()
CHECK_TIKTOK_URL = os.getenv("CHECK_TIKTOK_URL", "").strip()
CHECK_FACEBOOK_URL = os.getenv("CHECK_FACEBOOK_URL", "").strip()
STARTUP_CHECKS_REQUIRED = os.getenv("STARTUP_CHECKS_REQUIRED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "production").strip()
SENTRY_RELEASE = os.getenv("SENTRY_RELEASE", "").strip()

_allowed_raw = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS: set[int] = {
    int(cid.strip()) for cid in _allowed_raw.split(",") if cid.strip().lstrip("-").isdigit()
}


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
    re.compile(r"https?://(?:[\w-]+\.)?facebook\.com/\S+", re.IGNORECASE),
    re.compile(r"https?://fb\.watch/\S+", re.IGNORECASE),
]


def extract_supported_url(text: str) -> str | None:
    for pattern in SUPPORTED_URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).rstrip(".,);")
    return None


def exceeds_short_limit(duration: int) -> bool:
    """True, если видео длиннее допустимой длины «короткого» ролика.

    Применяется ко всем поддерживаемым платформам (YouTube, TikTok, Facebook):
    бот скачивает только короткие видео (shorts/reels), а не полноразмерные.
    Длительность 0 (неизвестна) считается допустимой и не отклоняется здесь.
    """
    return duration > MAX_SHORT_DURATION_SEC


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
        LinkCheck("facebook", CHECK_FACEBOOK_URL),
    ]


def run_check(check: LinkCheck) -> LinkCheckResult:
    from downloader.core import DownloadError, UnsupportedContentError, fetch_info

    if not check.url:
        return LinkCheckResult(check.service, False, "missing url")

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
    return check.service == "tiktok" and (
        "http error 403" in message or "forbidden" in message
    )


def print_results(results: list[LinkCheckResult]) -> None:
    print_startup_results(results)


def result_exit_code(results: list[LinkCheckResult]) -> int:
    if any(result.message == "missing url" for result in results):
        return 2
    if any(not result.ok and result.blocks_startup for result in results):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Entrypoints
# --------------------------------------------------------------------------- #

def configure_logging() -> None:
    configure_shared_logging(
        "INFO",
        quiet_loggers=("yt_dlp", "httpx", "httpcore"),
    )


def run_startup_then_bot() -> int:
    from downloader.bot import main as run_bot

    configure_logging()
    init_error_tracking("downloader-startup")

    logger.info("Running startup link checks.")
    results = run_checks()
    print_results(results)
    check_exit_code = result_exit_code(results)
    flush_error_tracking()

    if check_exit_code != 0:
        if STARTUP_CHECKS_REQUIRED:
            logger.error("Startup link checks failed with exit code %s.", check_exit_code)
            return check_exit_code
        logger.warning(
            "Startup link checks failed with exit code %s. Continuing because STARTUP_CHECKS_REQUIRED=0.",
            check_exit_code,
        )

    return run_bot()


def main() -> int:
    return run_startup_then_bot()


if __name__ == "__main__":
    raise SystemExit(main())
