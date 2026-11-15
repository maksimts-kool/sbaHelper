"""Shared fixtures. Every test gets default settings, a private cookie directory,
temp directory and database path, so nothing depends on the developer's environment."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sbahelper import cookies
from sbahelper.config import settings


@pytest.fixture(autouse=True)
def default_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # Outside tmp_path, so tests that list tmp_path do not see it.
    work = tmp_path_factory.mktemp("work-root")
    for name, value in {
        "token": "123456:TEST",
        "access_policy": "open",
        "allowed_chat_ids": frozenset(),
        "admin_ids": frozenset(),
        "tz": ZoneInfo("Europe/Tallinn"),
        "max_file_size_bytes": 45_000_000,
        "max_duration_sec": 300,
        "cookies_dir": tmp_path / "cookies",
        "check_urls": {"tiktok": "", "youtube": ""},
        "cookies_refresh_hours": 4,
        "stats_enabled": True,
        "stats_db_path": tmp_path / "stats.db",
        "stats_weekday": 6,
        "stats_time": time(20),
        "stats_retention_days": 400,
        "max_links_per_message": 3,
        "max_active_downloads": 2,
        "max_queued_downloads": 5,
        "max_pending_per_user": 3,
        "max_pending_per_chat": 8,
        "user_links_per_minute": 10,
        "download_timeout_sec": 240,
        "temp_dir": work,
        "cache_ttl_days": 30,
        "drop_pending_updates": True,
        "heartbeat_path": tmp_path / "heartbeat",
    }.items():
        monkeypatch.setattr(settings, name, value)
    # Cookie health and import generations are process-wide.
    monkeypatch.setattr(cookies, "_generation", {})
    monkeypatch.setattr(cookies, "_health", {})
