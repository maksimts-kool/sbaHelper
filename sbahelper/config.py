"""Settings, read once from the environment.

Defaults live here and only here: the compose files pass variables through
without repeating them. A malformed value falls back to its default and is
reported as a warning at startup instead of crashing the container.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class Env:
    """Typed readers over `os.environ` that collect problems instead of raising."""

    def __init__(self) -> None:
        self.issues: list[str] = []

    def _invalid[T](self, name: str, raw: str, default: T, shown: object = None) -> T:
        self.issues.append(f"Invalid {name}={raw!r}, using {shown or default}.")
        return default

    def text(self, name: str, default: str = "") -> str:
        return (os.getenv(name) or "").strip() or default

    def integer(self, name: str, default: int, *, low: int = 0, high: int | None = None) -> int:
        raw = self.text(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            return self._invalid(name, raw, default)
        if value < low or (high is not None and value > high):
            return self._invalid(name, raw, default)
        return value

    def flag(self, name: str, default: bool) -> bool:
        raw = self.text(name).lower()
        return default if not raw else raw not in {"0", "false", "no", "off"}

    def ids(self, name: str) -> frozenset[int]:
        ids: set[int] = set()
        for part in self.text(name).split(","):
            if part := part.strip():
                try:
                    ids.add(int(part))
                except ValueError:
                    self.issues.append(f"Ignoring {part!r} in {name}: not a numeric id.")
        return frozenset(ids)

    def clock(self, name: str, default: time) -> time:
        raw = self.text(name)
        if not raw:
            return default
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
        if match and int(match[1]) < 24 and int(match[2]) < 60:
            return time(int(match[1]), int(match[2]))
        return self._invalid(name, raw, default, default.strftime("%H:%M"))

    def zone(self, name: str, default: str) -> tzinfo:
        raw = self.text(name, default)
        try:
            return ZoneInfo(raw)
        except ZoneInfoNotFoundError, ValueError:
            return self._invalid(name, raw, ZoneInfo(default), default)

    def choice(self, name: str, default: str, choices: tuple[str, ...]) -> str:
        raw = self.text(name, default).upper()
        return raw if raw in choices else self._invalid(name, raw, default)


@dataclass(slots=True)
class Settings:
    token: str
    # Empty means the bot answers in every chat.
    allowed_chat_ids: frozenset[int]
    # Receive error alerts, may upload cookies and run /cookies and /check.
    admin_ids: frozenset[int]
    log_level: str
    tz: tzinfo
    max_file_size_mb: int
    max_duration_sec: int
    cookies_dir: Path
    # Platform → a known-good video the startup check downloads. Empty URLs are skipped.
    check_urls: dict[str, str]
    stats_enabled: bool
    stats_db_path: Path
    # `datetime.weekday()` numbering: 0 = Monday … 6 = Sunday.
    stats_weekday: int
    stats_time: time
    # 0 keeps rows forever.
    stats_retention_days: int
    # Problems found while reading the environment, logged once logging is set up.
    issues: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Settings:
        env = Env()
        return cls(
            token=env.text("DOWNLOADER_BOT_TOKEN"),
            allowed_chat_ids=env.ids("ALLOWED_CHAT_IDS"),
            admin_ids=env.ids("ADMIN_IDS"),
            log_level=env.choice("LOG_LEVEL", "INFO", LOG_LEVELS),
            tz=env.zone("TZ", "Europe/Tallinn"),
            # The Telegram bot API refuses uploads over 50 MB.
            max_file_size_mb=env.integer("MAX_FILE_SIZE_MB", 50, low=1),
            max_duration_sec=env.integer("MAX_SHORT_DURATION_SEC", 300, low=1),
            cookies_dir=Path(env.text("COOKIES_DIR", "/data/cookies")),
            check_urls={
                "tiktok": env.text("CHECK_TIKTOK_URL"),
                "youtube": env.text("CHECK_YOUTUBE_URL"),
            },
            stats_enabled=env.flag("STATS_ENABLED", True),
            stats_db_path=Path(env.text("STATS_DB_PATH", "/data/downloader_stats.db")),
            stats_weekday=env.integer("STATS_WEEKLY_WEEKDAY", 6, high=6),
            stats_time=env.clock("STATS_WEEKLY_TIME", time(20)),
            stats_retention_days=env.integer("STATS_RETENTION_DAYS", 400),
            issues=tuple(env.issues),
        )


settings = Settings.from_env()
