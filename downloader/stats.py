"""
Статистика загрузок: SQLite-хранилище событий и недельная агрегация.

Записывается только успешно отправленное видео — одна строка на отправку.
Из этих строк собирается сводка за неделю: таблица лидеров, разбивка по
площадкам и «хит недели». Текст сообщения строится в `downloader/formatting.py`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# Сколько человек показываем поимённо; остальные сворачиваются в «ещё N».
TOP_USERS_LIMIT = 3

# Пробелы (Zs/Zl/Zp), управляющие и форматирующие символы (Cc/Cf), комбинирующие
# знаки без собственной ширины (Mn/Me) — всё это Telegram рисует пустотой.
_BLANK_CATEGORIES = frozenset({"Cc", "Cf", "Mn", "Me", "Zs", "Zl", "Zp"})
# А эти Unicode считает буквами или символами, хотя глиф у них пустой. Из таких
# и собирают «невидимые» имена, чтобы висеть первым в списке участников.
# Записаны кодами намеренно: в исходнике их иначе не видно.
_BLANK_CHARS = frozenset(
    # HANGUL CHOSEONG/JUNGSEONG FILLER, HANGUL FILLER, HALFWIDTH HANGUL FILLER,
    # BRAILLE PATTERN BLANK, MONGOLIAN VOWEL SEPARATOR, KHMER INHERENT AQ/AA.
    "\u115f\u1160\u3164\uffa0\u2800\u180e\u17b4\u17b5"
)

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


# --------------------------------------------------------------------------- #
#  Модели                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DownloadEvent:
    chat_id: int
    user_id: int
    user_name: str
    # `@username`, если он у человека есть — из него собирается упоминание в сводке.
    username: str | None
    platform: str
    duration_sec: int = 0
    size_bytes: int = 0
    title: str = ""
    uploader: str = ""
    view_count: int | None = None


@dataclass(frozen=True)
class UserTally:
    name: str
    downloads: int
    # Есть — показываем упоминанием, и подпись подтянется из аккаунта сама.
    username: str | None = None


@dataclass(frozen=True)
class PlatformTally:
    platform: str
    downloads: int


@dataclass(frozen=True)
class TopVideo:
    title: str
    uploader: str
    view_count: int


@dataclass(frozen=True)
class WeeklyStats:
    period_start: datetime
    period_end: datetime
    total_downloads: int
    total_duration_sec: int
    total_size_bytes: int
    top_users: tuple[UserTally, ...]
    # Участники, не попавшие в топ, и сколько видео они скачали вместе.
    other_users: int
    other_downloads: int
    platforms: tuple[PlatformTally, ...]
    top_video: TopVideo | None


# --------------------------------------------------------------------------- #
#  Имена                                                                       #
# --------------------------------------------------------------------------- #


def has_visible_text(value: str) -> bool:
    """Останется ли хоть один видимый символ, если убрать пробелы и пустые глифы.

    Простой `strip()` тут не работает: HANGUL FILLER (U+3164) Unicode относит
    к буквам (категория `Lo`), а VARIATION SELECTOR-15 (U+FE0E) — к
    комбинирующим знакам, и `isspace()` для обоих даёт `False`. При этом имя,
    собранное из них, Telegram показывает пустым.
    """
    return any(
        char not in _BLANK_CHARS and unicodedata.category(char) not in _BLANK_CATEGORIES
        for char in value
    )


def display_user_name(raw: str, user_id: int) -> str:
    """Подпись для таблицы лидеров: невидимое имя заменяем на что-то читаемое."""
    if has_visible_text(raw):
        return raw
    return f"Участник {user_id}" if user_id else "Неизвестно"


# --------------------------------------------------------------------------- #
#  Время                                                                       #
# --------------------------------------------------------------------------- #


def resolve_timezone(name: str) -> tzinfo:
    """Часовой пояс по названию из IANA; при неудаче — UTC, чтобы не падать."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r, falling back to UTC.", name)
        return timezone.utc


def week_period(now: datetime) -> tuple[datetime, datetime]:
    """Текущая неделя: с понедельника 00:00 до `now` в том же часовом поясе."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=now.weekday()), now


def sunday_based_weekday(monday_based: int) -> int:
    """`datetime.weekday()` считает от понедельника, JobQueue — от воскресенья."""
    return (monday_based + 1) % 7


def _utc_text(moment: datetime) -> str:
    """Метка времени для SQLite: UTC ISO-8601 без микросекунд, сравнимая как текст."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
#  Агрегация                                                                   #
# --------------------------------------------------------------------------- #


def aggregate_weekly_stats(
    rows: Sequence[tuple[Any, ...]],
    *,
    period_start: datetime,
    period_end: datetime,
) -> WeeklyStats:
    """Собирает сводку из строк вида, который отдаёт `StatsStore.weekly_stats`."""
    user_names: dict[int, str] = {}
    user_usernames: dict[int, str | None] = {}
    user_counts: Counter[int] = Counter()
    platform_counts: Counter[str] = Counter()
    total_duration = 0
    total_size = 0
    top_video: TopVideo | None = None

    for (
        user_id,
        user_name,
        username,
        platform,
        duration_sec,
        size_bytes,
        title,
        uploader,
        views,
    ) in rows:
        user_key = int(user_id)
        # Имя могло измениться — оставляем последнее известное. Подменяем здесь,
        # а не только при записи, чтобы старые строки тоже не выглядели пустыми.
        user_names[user_key] = display_user_name(str(user_name), user_key)
        # У старых строк колонки ещё нет, а @username мог и появиться за неделю:
        # запоминаем последний непустой, иначе останемся с именем.
        user_usernames[user_key] = str(username) if username else user_usernames.get(user_key)
        user_counts[user_key] += 1
        platform_counts[str(platform)] += 1
        total_duration += int(duration_sec or 0)
        total_size += int(size_bytes or 0)

        if views is not None and (top_video is None or int(views) > top_video.view_count):
            top_video = TopVideo(str(title), str(uploader), int(views))

    ranked = sorted(user_counts.items(), key=lambda item: (-item[1], user_names[item[0]].lower()))
    tail = ranked[TOP_USERS_LIMIT:]

    return WeeklyStats(
        period_start=period_start,
        period_end=period_end,
        total_downloads=len(rows),
        total_duration_sec=total_duration,
        total_size_bytes=total_size,
        top_users=tuple(
            UserTally(user_names[user_id], count, user_usernames.get(user_id))
            for user_id, count in ranked[:TOP_USERS_LIMIT]
        ),
        other_users=len(tail),
        other_downloads=sum(count for _, count in tail),
        platforms=tuple(
            PlatformTally(platform, count)
            for platform, count in sorted(
                platform_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        top_video=top_video,
    )


def empty_weekly_stats(period_start: datetime, period_end: datetime) -> WeeklyStats:
    return aggregate_weekly_stats((), period_start=period_start, period_end=period_end)


# --------------------------------------------------------------------------- #
#  Хранилище                                                                   #
# --------------------------------------------------------------------------- #


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` не трогает уже существующую базу — дописываем сами.

    Колонка допускает NULL, поэтому у строк, записанных до неё, `@username`
    просто остаётся пустым — в сводке такой участник останется под именем.
    """
    present = {row[1] for row in conn.execute("PRAGMA table_info(downloads)")}
    if "username" not in present:
        conn.execute("ALTER TABLE downloads ADD COLUMN username TEXT")


class StatsStore:
    """Одна строка на успешно отправленное видео, в одном файле SQLite."""

    def __init__(self, path: str) -> None:
        self._path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            _add_missing_columns(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(self, event: DownloadEvent, *, at: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO downloads (created_at, chat_id, user_id, user_name, username, "
                "platform, duration_sec, size_bytes, title, uploader, view_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc_text(at or datetime.now(timezone.utc)),
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
        """Чаты, в которых за период что-то скачали."""
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
                "SELECT user_id, user_name, username, platform, duration_sec, size_bytes, "
                "title, uploader, view_count FROM downloads "
                "WHERE chat_id = ? AND created_at >= ? AND created_at < ? ORDER BY created_at",
                (chat_id, _utc_text(start), _utc_text(end)),
            ).fetchall()
        return aggregate_weekly_stats(rows, period_start=start, period_end=end)

    def prune(self, *, older_than: datetime) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM downloads WHERE created_at < ?", (_utc_text(older_than),)
            )
            return cursor.rowcount
