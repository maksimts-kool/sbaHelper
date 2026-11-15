"""Smoke checks: download a known-good video per platform and throw it away.

Run shortly after startup and on the admin /check command. They exercise the
same path as a real link (extraction, format selection, ffmpeg merge, media
checks), so a broken extractor, rejected cookies or a missing ffmpeg shows up
right away. Failures are logged at ERROR and reach the admins. The last result
per platform is kept in the database for /status.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sbahelper.config import settings
from sbahelper.db import Database
from sbahelper.download import DownloadError, download_video
from sbahelper.redact import safe_url

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CheckResult:
    platform: str
    ok: bool
    detail: str


def _state_key(platform: str) -> str:
    return f"check:{platform}"


def save_result(db: Database | None, result: CheckResult) -> None:
    if db is None:
        return
    value = json.dumps({"ok": result.ok, "at": datetime.now(UTC).isoformat()})
    try:
        db.set_state(_state_key(result.platform), value)
    except sqlite3.Error as error:
        log.warning("Check result not saved. Platform=%s Error=%s", result.platform, error)


def last_results(db: Database | None) -> dict[str, tuple[bool, datetime]]:
    results: dict[str, tuple[bool, datetime]] = {}
    if db is None:
        return results
    for platform in settings.check_urls:
        try:
            raw = db.get_state(_state_key(platform))
            if raw:
                data = json.loads(raw)
                results[platform] = (bool(data["ok"]), datetime.fromisoformat(data["at"]))
        except (sqlite3.Error, ValueError, KeyError, TypeError) as error:
            log.debug("Check result unreadable. Platform=%s Error=%s", platform, error)
    return results


def run_check(platform: str, url: str, db: Database | None = None) -> CheckResult:
    started = time.monotonic()
    try:
        result = download_video(url)
    except DownloadError as error:
        log.error(
            "Check failed. Platform=%s URL=%s Code=%s Error=%s",
            platform,
            safe_url(url),
            error.code,
            error.reason,
        )
        outcome = CheckResult(platform, False, error.reason)
        save_result(db, outcome)
        return outcome
    except Exception as error:
        log.exception("Check crashed. Platform=%s URL=%s", platform, safe_url(url))
        outcome = CheckResult(platform, False, f"{type(error).__name__}: {error}")
        save_result(db, outcome)
        return outcome

    result.cleanup()
    elapsed = time.monotonic() - started
    size_mb = result.size_bytes / 1_000_000
    log.info(
        "Check passed. Platform=%s Size=%d Time=%.1fs Video=%s",
        platform,
        result.size_bytes,
        elapsed,
        result.info.key,
    )
    outcome = CheckResult(
        platform, True, f"{result.info.title} ({size_mb:.1f} MB, {elapsed:.0f} s)"
    )
    save_result(db, outcome)
    return outcome


def run_checks(db: Database | None = None) -> list[CheckResult]:
    """Every configured check; platforms without a CHECK_*_URL are skipped."""
    return [run_check(platform, url, db) for platform, url in settings.check_urls.items() if url]
