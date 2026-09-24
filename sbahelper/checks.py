"""Smoke checks: download a known-good video per platform and throw it away.

Run shortly after startup and on the admin /check command. They exercise the
same path as a real link (extraction, format selection, ffmpeg merge, audio
and size checks), so a broken extractor, expired cookies or a missing ffmpeg
shows up right away. Failures are logged at ERROR and reach the admins.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sbahelper.config import settings
from sbahelper.download import DownloadError, download_video

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CheckResult:
    platform: str
    ok: bool
    detail: str


def run_check(platform: str, url: str) -> CheckResult:
    started = time.monotonic()
    try:
        result = download_video(url)
    except DownloadError as error:
        log.error("Check failed. Platform=%s URL=%s Error=%s", platform, url, error.reason)
        return CheckResult(platform, False, error.reason)
    except Exception as error:
        log.exception("Check crashed. Platform=%s URL=%s", platform, url)
        return CheckResult(platform, False, f"{type(error).__name__}: {error}")

    result.cleanup()
    elapsed = time.monotonic() - started
    size_mb = result.size_bytes / 1024**2
    log.info(
        "Check passed. Platform=%s Size=%.1fMB Time=%.1fs Title=%s",
        platform,
        size_mb,
        elapsed,
        result.info.title,
    )
    return CheckResult(platform, True, f"{result.info.title} ({size_mb:.1f} MB, {elapsed:.0f} s)")


def run_checks() -> list[CheckResult]:
    """Every configured check; platforms without a CHECK_*_URL are skipped."""
    return [run_check(platform, url) for platform, url in settings.check_urls.items() if url]
