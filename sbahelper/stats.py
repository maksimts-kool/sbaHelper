"""Download log in SQLite and its weekly aggregation.

One row per video actually sent to a chat; rejected links and failures are
not stored. `texts.weekly_stats` turns a `WeeklyStats` into the message.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# People shown by name; everyone else is folded into "and N more".
TOP_USERS = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    user_name    TEXT    NOT NULL,
    username     TEXT,
    platform     TEXT    NOT NULL,
    duration_sec INTEGER NOT NULL,
    size_bytes   INTEGER NOT NULL,
    title        TEXT    NOT NULL,
    uploader     TEXT    NOT NULL,
    view_count   INTEGER
);
CREATE INDEX IF NOT EXISTS downloads_chat_created_idx ON downloads (chat_id, created_at);
CREATE INDEX IF NOT EXISTS downloads_created_idx ON downloads (created_at);
"""
_ROW_COLUMNS = (
    "user_id, user_name, username, platform, duration_sec, size_bytes, title, uploader, view_count"
)


@dataclass(frozen=True, slots=True)
class DownloadEvent:
    chat_id: int
    user_id: int
    user_name: str
    # Rendered as a real mention in the weekly summary when present.
    username: str | None
    platform: str
    duration_sec: int = 0
    size_bytes: int = 0
    title: str = ""
    uploader: str = ""
    view_count: int | None = None


@dataclass(frozen=True, slots=True)
class UserTally:
    user_id: int
    name: str
    downloads: int
    username: str | None = None


@dataclass(frozen=True, slots=True)
class PlatformTally:
    platform: str
    downloads: int


@dataclass(frozen=True, slots=True)
class TopVideo:
    title: str
    uploader: str
    view_count: int


@dataclass(frozen=True, slots=True)
class WeeklyStats:
    period_start: datetime
    period_end: datetime
    total_downloads: int
    total_duration_sec: int
    total_size_bytes: int
    top_users: tuple[UserTally, ...]
    # People outside the top and how many videos they downloaded together.
    other_users: int
    other_downloads: int
    platforms: tuple[PlatformTally, ...]
    top_video: TopVideo | None


def week_period(now: datetime) -> tuple[datetime, datetime]:
    """This week so far: Monday 00:00 up to `now`, in `now`'s timezone."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=now.weekday()), now


def _utc_text(moment: datetime) -> str:
    """UTC ISO-8601 without microseconds: sorts correctly as text in SQLite."""
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(microsecond=0).isoformat()


def aggregate(
    rows: Sequence[tuple[Any, ...]], *, period_start: datetime, period_end: datetime
) -> WeeklyStats:
    """Summarise rows in `_ROW_COLUMNS` order, oldest first."""
    names: dict[int, str] = {}
    usernames: dict[int, str | None] = {}
    per_user: Counter[int] = Counter()
    per_platform: Counter[str] = Counter()
    total_duration = total_size = 0
    top_video: TopVideo | None = None

    for user_id, name, username, platform, duration, size, title, uploader, views in rows:
        user_id = int(user_id)
        # Rows are oldest first, so the latest name wins. A username may appear
        # or disappear during the week: keep the latest one that is known.
        names[user_id] = str(name)
        usernames[user_id] = str(username) if username else usernames.get(user_id)
        per_user[user_id] += 1
        per_platform[str(platform)] += 1
        total_duration += int(duration or 0)
        total_size += int(size or 0)
        if views is not None and (top_video is None or int(views) > top_video.view_count):
            top_video = TopVideo(str(title), str(uploader), int(views))

    ranked = sorted(per_user.items(), key=lambda item: (-item[1], names[item[0]].lower()))
    rest = ranked[TOP_USERS:]
    return WeeklyStats(
        period_start=period_start,
        period_end=period_end,
        total_downloads=len(rows),
        total_duration_sec=total_duration,
        total_size_bytes=total_size,
        top_users=tuple(
            UserTally(user_id, names[user_id], count, usernames[user_id])
            for user_id, count in ranked[:TOP_USERS]
        ),
        other_users=len(rest),
        other_downloads=sum(count for _, count in rest),
        platforms=tuple(
            PlatformTally(platform, count)
            for platform, count in sorted(per_platform.items(), key=lambda i: (-i[1], i[0]))
        ),
        top_video=top_video,
    )


class StatsStore:
    """One SQLite file; a short-lived connection per call, so it is thread-safe."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Databases from before usernames were stored lack the column.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(downloads)")}
            if "username" not in columns:
                conn.execute("ALTER TABLE downloads ADD COLUMN username TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path)) as conn, conn:
            yield conn

    def record(self, event: DownloadEvent, *, at: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO downloads (created_at, chat_id, {_ROW_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc_text(at or datetime.now(UTC)),
                    event.chat_id,
                    event.user_id,
                    event.user_name,
                    event.username,
                    event.platform,
                    event.duration_sec,
                    event.size_bytes,
                    event.title,
                    event.uploader,
                    event.view_count,
                ),
            )

    def active_chat_ids(self, start: datetime, end: datetime) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT chat_id FROM downloads "
                "WHERE created_at >= ? AND created_at < ? ORDER BY chat_id",
                (_utc_text(start), _utc_text(end)),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def weekly_stats(self, *, chat_id: int, start: datetime, end: datetime) -> WeeklyStats:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM downloads "
                "WHERE chat_id = ? AND created_at >= ? AND created_at < ? ORDER BY created_at",
                (chat_id, _utc_text(start), _utc_text(end)),
            ).fetchall()
        return aggregate(rows, period_start=start, period_end=end)

    def prune(self, *, older_than: datetime) -> int:
        with self._connect() as conn:
            return conn.execute(
                "DELETE FROM downloads WHERE created_at < ?", (_utc_text(older_than),)
            ).rowcount
