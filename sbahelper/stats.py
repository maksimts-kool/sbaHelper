"""Download log in SQLite and its aggregation per day, week or month.

One row per video actually sent to a chat; rejected links and failures are
not stored. Cached deliveries count like fresh downloads. A user who opted
out of statistics is stored without id or name (`user_id = 0`), so chat
totals stay right without personal data. `texts.period_stats` turns a
`PeriodStats` into the message.

Periods are calendar periods in the bot's timezone, from their start up to
"now": today from 00:00, this week from Monday 00:00, this month from the
1st 00:00. Boundaries are computed on local wall-clock time, so DST changes
neither drop nor double-count an hour. The scheduled weekly summary covers
Monday 00:00 up to the moment it is sent (as it always did).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sbahelper.db import Database, utc_text

# People shown by name; everyone else is folded into "and N more".
TOP_USERS = 3
PERIODS = ("day", "week", "month")

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
class PeriodStats:
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
    # Rows without a person (opted out, deleted data, channel posts).
    anonymous_downloads: int = 0


WeeklyStats = PeriodStats


def period_bounds(period: str, now: datetime) -> tuple[datetime, datetime]:
    """Start of the calendar day/week/month containing `now`, and `now`.

    `now` must be timezone-aware; arithmetic stays in its zone, so midnight is
    local midnight on both sides of a DST change.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        start = midnight
    elif period == "month":
        start = midnight.replace(day=1)
    elif period == "week":
        start = midnight - timedelta(days=now.weekday())
    else:
        raise ValueError(f"unknown period {period!r}")
    # Re-attach the zone so the offset is the one valid at the new wall time.
    if start.tzinfo is not None:
        start = start.replace(tzinfo=None).replace(tzinfo=now.tzinfo)
    return start, now


def week_period(now: datetime) -> tuple[datetime, datetime]:
    """This week so far: Monday 00:00 up to `now`, in `now`'s timezone."""
    return period_bounds("week", now)


def aggregate(
    rows: Sequence[tuple[Any, ...]], *, period_start: datetime, period_end: datetime
) -> PeriodStats:
    """Summarise rows in `_ROW_COLUMNS` order, oldest first."""
    names: dict[int, str] = {}
    usernames: dict[int, str | None] = {}
    per_user: Counter[int] = Counter()
    per_platform: Counter[str] = Counter()
    total_duration = total_size = anonymous = 0
    top_video: TopVideo | None = None

    for user_id, name, username, platform, duration, size, title, uploader, views in rows:
        user_id = int(user_id)
        per_platform[str(platform)] += 1
        total_duration += int(duration or 0)
        total_size += int(size or 0)
        if views is not None and (top_video is None or int(views) > top_video.view_count):
            top_video = TopVideo(str(title), str(uploader), int(views))
        if not user_id:
            anonymous += 1
            continue
        # Rows are oldest first, so the latest name wins. A username may appear
        # or disappear during the week: keep the latest one that is known.
        names[user_id] = str(name)
        usernames[user_id] = str(username) if username else usernames.get(user_id)
        per_user[user_id] += 1

    ranked = sorted(per_user.items(), key=lambda item: (-item[1], names[item[0]].lower()))
    rest = ranked[TOP_USERS:]
    return PeriodStats(
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
        anonymous_downloads=anonymous,
    )


class StatsStore:
    """Statistics over the shared `Database`; safe to call from worker threads."""

    def __init__(self, db: Database | Path) -> None:
        self.db = db if isinstance(db, Database) else Database(db)

    def record(self, event: DownloadEvent, *, at: datetime | None = None) -> None:
        with self.db.transaction() as conn:
            opted_out = event.user_id and conn.execute(
                "SELECT stats_opt_out FROM user_settings WHERE user_id = ?", (event.user_id,)
            ).fetchone() in ((1,),)
            user_id, name, username = (
                (0, "", None) if opted_out else (event.user_id, event.user_name, event.username)
            )
            conn.execute(
                f"INSERT INTO downloads (created_at, chat_id, {_ROW_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    utc_text(at),
                    event.chat_id,
                    user_id,
                    name,
                    username,
                    event.platform,
                    event.duration_sec,
                    event.size_bytes,
                    event.title,
                    event.uploader,
                    event.view_count,
                ),
            )

    def active_chat_ids(self, start: datetime, end: datetime) -> list[int]:
        """Chats with downloads in the period, minus chats the bot was removed from."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT d.chat_id FROM downloads d "
                "LEFT JOIN chat_settings c ON c.chat_id = d.chat_id "
                "WHERE d.created_at >= ? AND d.created_at < ? AND COALESCE(c.active, 1) = 1 "
                "ORDER BY d.chat_id",
                (utc_text(start), utc_text(end)),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def period_stats(self, *, chat_id: int, start: datetime, end: datetime) -> PeriodStats:
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM downloads "
                "WHERE chat_id = ? AND created_at >= ? AND created_at < ? ORDER BY created_at, id",
                (chat_id, utc_text(start), utc_text(end)),
            ).fetchall()
        return aggregate(rows, period_start=start, period_end=end)

    weekly_stats = period_stats

    def prune(self, *, older_than: datetime) -> int:
        return self.db.prune_in_batches(
            "SELECT rowid FROM downloads WHERE created_at < ?", "downloads", (utc_text(older_than),)
        )

    # -- personal data ------------------------------------------------------- #

    def user_row_count(self, user_id: int) -> int:
        with self.db.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM downloads WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
            )

    def delete_user(self, user_id: int) -> int:
        """Remove every statistics row of one person in every chat, and their settings."""
        if not user_id:
            return 0
        with self.db.transaction() as conn:
            removed = conn.execute("DELETE FROM downloads WHERE user_id = ?", (user_id,)).rowcount
            conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        return removed

    def migrate_chat(self, old_chat_id: int, new_chat_id: int) -> int:
        """A group became a supergroup: its history and settings follow the new id."""
        with self.db.transaction() as conn:
            moved = conn.execute(
                "UPDATE downloads SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id)
            ).rowcount
            exists = conn.execute(
                "SELECT 1 FROM chat_settings WHERE chat_id = ?", (new_chat_id,)
            ).fetchone()
            if exists:
                conn.execute("DELETE FROM chat_settings WHERE chat_id = ?", (old_chat_id,))
            else:
                conn.execute(
                    "UPDATE chat_settings SET chat_id = ? WHERE chat_id = ?",
                    (new_chat_id, old_chat_id),
                )
        return moved


def open_store(path: Path) -> StatsStore:
    """Open (and migrate) the database; raises `sqlite3.Error`/`OSError`/`SchemaTooNew`."""
    return StatsStore(Database(path))
