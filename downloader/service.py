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

import sentry_sdk
from dotenv import load_dotenv


load_dotenv()
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

DOWNLOADER_BOT_TOKEN = os.getenv("DOWNLOADER_BOT_TOKEN", "")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_DURATION_SEC = int(os.getenv("MAX_DURATION_SEC", "600"))
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

_FACEBOOK_HOST = r"(?:[\w-]+\.)?facebook\.com"
_FACEBOOK_PATH_SUFFIX = r"(?:[/?][^\s]*)?"
_YOUTUBE_HOST = r"(?:(?:www|m)\.)?youtube\.com"

SUPPORTED_URL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"https?://vm\.tiktok\.com/\S+", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?tiktok\.com/\S+", re.IGNORECASE),
    re.compile(rf"https?://{_YOUTUBE_HOST}/shorts/\S+", re.IGNORECASE),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/watch(?:/live)?/\?(?=[^\s#]*\b(?:v|video_id)=[^&#\s]+)[^\s#]+",
        re.IGNORECASE,
    ),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/video(?:/video)?\.php\?(?=[^\s#]*\b(?:v|video_id)=[^&#\s]+)[^\s#]+",
        re.IGNORECASE,
    ),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/story\.php\?(?=[^\s#]*\bstory_fbid=[^&#\s]+)[^\s#]+",
        re.IGNORECASE,
    ),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/reel/[^/?#\s]+{_FACEBOOK_PATH_SUFFIX}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/(?:[^/?#\s]+/)?videos/(?:[^/?#\s]+/)?[^/?#\s]+{_FACEBOOK_PATH_SUFFIX}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/share/(?:r|v)/[^/?#\s]+{_FACEBOOK_PATH_SUFFIX}",
        re.IGNORECASE,
    ),
    re.compile(r"https?://fb\.watch/[^/?#\s]+(?:[/?][^\s]*)?", re.IGNORECASE),
]


def extract_supported_url(text: str) -> str | None:
    for pattern in SUPPORTED_URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).rstrip(".,);")
    return None


# --------------------------------------------------------------------------- #
# Error tracking
# --------------------------------------------------------------------------- #

_sentry_initialized = False


def init_error_tracking(service_name: str) -> bool:
    global _sentry_initialized

    if _sentry_initialized:
        sentry_sdk.set_tag("service", service_name)
        return True

    if not SENTRY_DSN:
        logger.debug("Sentry is not configured.")
        return False

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT or None,
        release=SENTRY_RELEASE or None,
        traces_sample_rate=0.0,
    )
    sentry_sdk.set_tag("service", service_name)
    _sentry_initialized = True
    logger.info("Sentry error tracking enabled for %s.", service_name)
    return True


def capture_exception(error: BaseException) -> None:
    if _sentry_initialized:
        sentry_sdk.capture_exception(error)


def flush_error_tracking(timeout: float = 2.0) -> None:
    if _sentry_initialized:
        sentry_sdk.flush(timeout=timeout)


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


def configured_checks() -> list[LinkCheck]:
    return [
        LinkCheck("youtube", CHECK_YOUTUBE_URL),
        LinkCheck("tiktok", CHECK_TIKTOK_URL),
        LinkCheck("facebook", CHECK_FACEBOOK_URL),
    ]


def run_check(check: LinkCheck) -> LinkCheckResult:
    from downloader.core import DownloadError, fetch_info

    if not check.url:
        return LinkCheckResult(check.service, False, "missing url")

    supported_url = extract_supported_url(check.url)
    if not supported_url:
        return LinkCheckResult(check.service, False, "unsupported url format")

    try:
        info = fetch_info(supported_url)
    except DownloadError as error:
        capture_exception(error)
        return LinkCheckResult(check.service, False, str(error))
    except Exception as error:
        capture_exception(error)
        logger.exception("Unexpected check error for %s", check.service)
        return LinkCheckResult(check.service, False, f"unexpected error: {error}")

    return LinkCheckResult(
        check.service,
        True,
        f"{info.title} by {info.uploader} ({info.duration}s)",
        info,
    )


def run_checks() -> list[LinkCheckResult]:
    return [run_check(check) for check in configured_checks()]


def print_results(results: list[LinkCheckResult]) -> None:
    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(f"{status} {result.service}: {result.message}")


def result_exit_code(results: list[LinkCheckResult]) -> int:
    if any(result.message == "missing url" for result in results):
        return 2
    if any(not result.ok for result in results):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Entrypoints
# --------------------------------------------------------------------------- #

def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
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
