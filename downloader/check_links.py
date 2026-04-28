"""
Metadata checks for the three supported services.
Run with: python -m downloader.check_links
"""
import logging
from dataclasses import dataclass

from downloader.config import CHECK_FACEBOOK_URL, CHECK_TIKTOK_URL, CHECK_YOUTUBE_URL
from downloader.core import DownloadError, VideoInfo, fetch_info
from downloader.error_tracking import capture_exception, flush_error_tracking, init_error_tracking
from downloader.url_support import extract_supported_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkCheck:
    service: str
    url: str


@dataclass(frozen=True)
class LinkCheckResult:
    service: str
    ok: bool
    message: str
    info: VideoInfo | None = None


def configured_checks() -> list[LinkCheck]:
    return [
        LinkCheck("youtube", CHECK_YOUTUBE_URL),
        LinkCheck("tiktok", CHECK_TIKTOK_URL),
        LinkCheck("facebook", CHECK_FACEBOOK_URL),
    ]


def run_check(check: LinkCheck) -> LinkCheckResult:
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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    init_error_tracking("downloader-check")

    results = run_checks()
    print_results(results)
    exit_code = result_exit_code(results)
    flush_error_tracking()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
