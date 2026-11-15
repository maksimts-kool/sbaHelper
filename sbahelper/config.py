"""Settings, read once from the environment.

Defaults live here and only here: the compose files pass variables through
without repeating them. A malformed value falls back to its default and is
reported as a warning at startup instead of crashing the container, with one
deliberate exception: a malformed chat allowlist fails *closed* (see
`_access_policy`), because falling back to "every chat" would silently open
the bot to everybody.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import time, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
ACCESS_POLICIES = ("open", "allowlist")

# The cloud Bot API documents 50 MB for new video uploads (sendVideo, multipart).
# Whether "MB" means 10^6 or 2^20 is not stated, so the hard ceiling is the
# smaller reading. https://core.telegram.org/bots/api#sendvideo
TELEGRAM_UPLOAD_CEILING_BYTES = 50_000_000
# Project choice, not a Telegram rule: headroom below the ceiling for
# multipart overhead and estimate errors.
DEFAULT_MAX_FILE_SIZE_BYTES = 45_000_000
MIN_FILE_SIZE_BYTES = 1_000_000


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
        raw = self.text(name).replace("_", "")
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

    def id_list(self, name: str) -> tuple[frozenset[int], bool]:
        """Numeric ids and whether any part was invalid (each one is reported)."""
        ids: set[int] = set()
        invalid = False
        for part in self.text(name).split(","):
            if part := part.strip():
                try:
                    ids.add(int(part))
                except ValueError:
                    invalid = True
                    self.issues.append(f"Ignoring {part!r} in {name}: not a numeric id.")
        return frozenset(ids), invalid

    def ids(self, name: str) -> frozenset[int]:
        return self.id_list(name)[0]

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


def _max_file_size(env: Env) -> int:
    """MAX_FILE_SIZE_BYTES, or the deprecated MAX_FILE_SIZE_MB read as 10^6 bytes."""
    if env.text("MAX_FILE_SIZE_BYTES"):
        return env.integer(
            "MAX_FILE_SIZE_BYTES",
            DEFAULT_MAX_FILE_SIZE_BYTES,
            low=MIN_FILE_SIZE_BYTES,
            high=TELEGRAM_UPLOAD_CEILING_BYTES,
        )
    if not env.text("MAX_FILE_SIZE_MB"):
        return DEFAULT_MAX_FILE_SIZE_BYTES
    megabytes = env.integer("MAX_FILE_SIZE_MB", 0, low=1)
    if not megabytes:
        return DEFAULT_MAX_FILE_SIZE_BYTES
    value = megabytes * 1_000_000
    env.issues.append(
        "MAX_FILE_SIZE_MB is deprecated: set MAX_FILE_SIZE_BYTES instead "
        f"(read as {min(value, TELEGRAM_UPLOAD_CEILING_BYTES)} bytes)."
    )
    if value > TELEGRAM_UPLOAD_CEILING_BYTES:
        env.issues.append(
            f"MAX_FILE_SIZE_MB={megabytes} is above the {TELEGRAM_UPLOAD_CEILING_BYTES}-byte "
            "cloud Bot API upload limit; capped."
        )
        return TELEGRAM_UPLOAD_CEILING_BYTES
    return max(value, MIN_FILE_SIZE_BYTES)


def _access_policy(env: Env) -> tuple[str, frozenset[int]]:
    """ACCESS_POLICY plus ALLOWED_CHAT_IDS, failing closed on anything ambiguous.

    Without ACCESS_POLICY the old behaviour is kept: an empty ALLOWED_CHAT_IDS
    means "every chat". A non-empty one always means an allowlist, even when
    some or all of its entries are invalid: those are dropped and reported,
    and an allowlist that ends up empty admits no group at all.
    """
    raw_ids = env.text("ALLOWED_CHAT_IDS")
    ids, _ = env.id_list("ALLOWED_CHAT_IDS")
    raw_policy = env.text("ACCESS_POLICY").lower()
    if not raw_policy:
        policy = "allowlist" if raw_ids else "open"
    elif raw_policy in ACCESS_POLICIES:
        policy = raw_policy
    else:
        env.issues.append(f"Invalid ACCESS_POLICY={raw_policy!r}, using allowlist (fail closed).")
        policy = "allowlist"
    if policy == "allowlist" and not ids:
        env.issues.append(
            "The chat allowlist is empty: only admins' private chats are served. "
            "Set ALLOWED_CHAT_IDS, or ACCESS_POLICY=open to serve every chat."
        )
    if policy == "open" and ids:
        env.issues.append("ACCESS_POLICY=open: ALLOWED_CHAT_IDS is ignored.")
    return policy, ids


@dataclass(slots=True)
class Settings:
    token: str
    # "open" answers in every chat; "allowlist" only in `allowed_chat_ids`
    # (and in admins' private chats).
    access_policy: str
    allowed_chat_ids: frozenset[int]
    # Receive error alerts, may upload cookies and run /cookies, /check and /status.
    admin_ids: frozenset[int]
    log_level: str
    tz: tzinfo
    # Exact bytes; one limit for format selection, download and the final check.
    max_file_size_bytes: int
    max_duration_sec: int
    cookies_dir: Path
    # Platform → a known-good video the startup check downloads. Empty URLs are skipped.
    check_urls: dict[str, str]
    # Open the CHECK_*_URL pages with the cookies this often; 0 = off.
    cookies_refresh_hours: int
    stats_enabled: bool
    # The SQLite database: statistics, Telegram file cache and settings.
    stats_db_path: Path
    # `datetime.weekday()` numbering: 0 = Monday … 6 = Sunday.
    stats_weekday: int
    stats_time: time
    # 0 keeps rows forever.
    stats_retention_days: int
    # Links taken from one message; the rest are ignored.
    max_links_per_message: int
    # Downloads running at once, and jobs allowed to wait for a free slot.
    max_active_downloads: int
    max_queued_downloads: int
    # Requests (queued or running) one user or one chat may have at a time.
    max_pending_per_user: int
    max_pending_per_chat: int
    # Links one user may send per minute; cached videos count too.
    user_links_per_minute: int
    # Wall-clock budget for one download (extraction, download, checks).
    download_timeout_sec: int
    # Where per-download work directories are created.
    temp_dir: Path
    # Telegram file_id cache lifetime; 0 disables the cache.
    cache_ttl_days: int
    # Skip updates that arrived while the bot was down (Telegram keeps them up to 24 h).
    drop_pending_updates: bool
    # Touched every few seconds while the bot is alive; read by the healthcheck.
    heartbeat_path: Path
    # Problems found while reading the environment, logged once logging is set up.
    issues: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Settings:
        env = Env()
        policy, allowed = _access_policy(env)
        temp_dir = Path(env.text("TEMP_DIR", tempfile.gettempdir()))
        return cls(
            token=env.text("DOWNLOADER_BOT_TOKEN"),
            access_policy=policy,
            allowed_chat_ids=allowed,
            admin_ids=env.ids("ADMIN_IDS"),
            log_level=env.choice("LOG_LEVEL", "INFO", LOG_LEVELS),
            tz=env.zone("TZ", "Europe/Tallinn"),
            max_file_size_bytes=_max_file_size(env),
            max_duration_sec=env.integer("MAX_SHORT_DURATION_SEC", 300, low=1),
            cookies_dir=Path(env.text("COOKIES_DIR", "/data/cookies")),
            check_urls={
                "tiktok": env.text("CHECK_TIKTOK_URL"),
                "youtube": env.text("CHECK_YOUTUBE_URL"),
            },
            cookies_refresh_hours=env.integer("COOKIES_REFRESH_HOURS", 4),
            stats_enabled=env.flag("STATS_ENABLED", True),
            stats_db_path=Path(env.text("STATS_DB_PATH", "/data/downloader_stats.db")),
            stats_weekday=env.integer("STATS_WEEKLY_WEEKDAY", 6, high=6),
            stats_time=env.clock("STATS_WEEKLY_TIME", time(20)),
            stats_retention_days=env.integer("STATS_RETENTION_DAYS", 400),
            max_links_per_message=env.integer("MAX_LINKS_PER_MESSAGE", 3, low=1, high=10),
            max_active_downloads=env.integer("MAX_ACTIVE_DOWNLOADS", 3, low=1, high=16),
            max_queued_downloads=env.integer("MAX_QUEUED_DOWNLOADS", 20, low=0, high=500),
            max_pending_per_user=env.integer("MAX_PENDING_PER_USER", 3, low=1, high=50),
            max_pending_per_chat=env.integer("MAX_PENDING_PER_CHAT", 8, low=1, high=100),
            user_links_per_minute=env.integer("USER_LINKS_PER_MINUTE", 10, low=1, high=600),
            download_timeout_sec=env.integer("DOWNLOAD_TIMEOUT_SEC", 240, low=30, high=3600),
            temp_dir=temp_dir,
            cache_ttl_days=env.integer("CACHE_TTL_DAYS", 30, high=3650),
            drop_pending_updates=env.flag("DROP_PENDING_UPDATES", True),
            heartbeat_path=Path(
                env.text("HEARTBEAT_PATH", str(Path(tempfile.gettempdir()) / "sbahelper.heartbeat"))
            ),
            issues=tuple(env.issues),
        )

    def chat_allowed(self, chat_id: int) -> bool:
        """The bot's own allowlist. Telegram's Privacy Mode is a separate, earlier filter."""
        if self.access_policy == "open":
            return True
        # An admin's private chat has the admin's user id.
        return chat_id in self.allowed_chat_ids or chat_id in self.admin_ids

    def describe_access(self) -> str:
        if self.access_policy == "open":
            return "open (every chat)"
        chats = ",".join(map(str, sorted(self.allowed_chat_ids))) or "none"
        return f"allowlist (chats {chats}; plus admins' private chats)"


settings = Settings.from_env()
