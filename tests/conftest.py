"""Shared fixtures. Every test gets default settings and a private cookie directory,
so nothing depends on the developer's environment."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sbahelper.config import settings


@pytest.fixture(autouse=True)
def default_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, value in {
        "allowed_chat_ids": frozenset(),
        "admin_ids": frozenset(),
        "tz": ZoneInfo("Europe/Tallinn"),
        "max_file_size_mb": 50,
        "max_duration_sec": 300,
        "cookies_dir": tmp_path / "cookies",
        "check_urls": {"tiktok": "", "youtube": ""},
        "stats_enabled": True,
        "stats_db_path": tmp_path / "stats.db",
        "stats_weekday": 6,
        "stats_time": time(20),
        "stats_retention_days": 400,
    }.items():
        monkeypatch.setattr(settings, name, value)
