import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sbahelper.stats import DownloadEvent, StatsStore, aggregate, week_period

START, END = datetime(2026, 7, 20, tzinfo=UTC), datetime(2026, 7, 27, tzinfo=UTC)


def row(user_id, name, platform="tiktok", duration=60, size=1024**2, views=None, username=None):
    return (user_id, name, username, platform, duration, size, "Видео", "author", views)


def stats(rows):
    return aggregate(rows, period_start=START, period_end=END)


def test_week_starts_on_monday_midnight() -> None:
    sunday = datetime(2026, 7, 26, 20, tzinfo=UTC)
    assert week_period(sunday) == (START, sunday)
    monday = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    assert week_period(monday)[0] == START


def test_totals_and_ranking() -> None:
    result = stats(
        [
            *[row(1, "Максим", duration=30, size=1_000_000)] * 3,
            *[row(2, "Аня", platform="youtube", duration=60, size=2_000_000)] * 2,
            row(3, "Пётр", duration=90, size=3_000_000),
        ]
    )
    assert result.total_downloads == 6
    assert result.total_duration_sec == 3 * 30 + 2 * 60 + 90
    assert result.total_size_bytes == 10_000_000
    assert [(t.name, t.downloads) for t in result.top_users] == [
        ("Максим", 3),
        ("Аня", 2),
        ("Пётр", 1),
    ]
    assert [(p.platform, p.downloads) for p in result.platforms] == [("tiktok", 4), ("youtube", 2)]


def test_people_beyond_the_top_are_folded() -> None:
    result = stats(
        [
            *[row(1, "A")] * 5,
            *[row(2, "B")] * 4,
            *[row(3, "C")] * 3,
            *[row(4, "D")] * 2,
            row(5, "E"),
        ]
    )
    assert [t.name for t in result.top_users] == ["A", "B", "C"]
    assert (result.other_users, result.other_downloads) == (2, 3)


def test_latest_name_and_known_username_win() -> None:
    result = stats([row(1, "Old", username="nick"), row(1, "New")])
    assert (result.top_users[0].name, result.top_users[0].username) == ("New", "nick")


def test_ties_are_ordered_by_name() -> None:
    assert [t.name for t in stats([row(2, "Яна"), row(1, "Артём")]).top_users] == ["Артём", "Яна"]


def test_top_video_is_the_most_viewed() -> None:
    result = stats([row(1, "A", views=10), row(1, "A", views=2_400_000), row(2, "B")])
    assert result.top_video is not None and result.top_video.view_count == 2_400_000
    assert stats([row(1, "A")]).top_video is None


# --------------------------------------------------------------------------- #
#  Store                                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(tmp_path: Path) -> StatsStore:
    return StatsStore(tmp_path / "nested" / "stats.db")


def record(store: StatsStore, chat_id: int, at: datetime, user_id: int = 1, **kwargs) -> None:
    event = DownloadEvent(
        chat_id, user_id, kwargs.pop("name", "A"), kwargs.pop("username", None), "tiktok", **kwargs
    )
    store.record(event, at=at)


def test_rows_survive_reopening(tmp_path: Path) -> None:
    record(StatsStore(tmp_path / "s.db"), -100, datetime(2026, 7, 21, tzinfo=UTC), username="nick")
    reopened = StatsStore(tmp_path / "s.db").weekly_stats(chat_id=-100, start=START, end=END)
    assert reopened.total_downloads == 1
    assert reopened.top_users[0].username == "nick"


def test_stats_are_per_chat_and_per_period(store: StatsStore) -> None:
    record(store, -100, START)
    record(store, -100, START - timedelta(seconds=1))
    record(store, -100, END)  # the end is exclusive
    record(store, -200, datetime(2026, 7, 22, 12))  # naive timestamps are UTC

    assert store.weekly_stats(chat_id=-100, start=START, end=END).total_downloads == 1
    assert store.weekly_stats(chat_id=-200, start=START, end=END).total_downloads == 1
    assert store.active_chat_ids(START, END) == [-200, -100]


def test_prune_removes_only_old_rows(store: StatsStore) -> None:
    record(store, -100, datetime(2026, 1, 1, tzinfo=UTC))
    record(store, -100, datetime(2026, 7, 21, tzinfo=UTC))

    assert store.prune(older_than=datetime(2026, 6, 1, tzinfo=UTC)) == 1
    assert store.weekly_stats(chat_id=-100, start=START, end=END).total_downloads == 1


def test_old_database_gets_the_username_column(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE downloads (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created_at TEXT NOT NULL, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
            "user_name TEXT NOT NULL, platform TEXT NOT NULL, duration_sec INTEGER NOT NULL, "
            "size_bytes INTEGER NOT NULL, title TEXT NOT NULL, uploader TEXT NOT NULL, "
            "view_count INTEGER)"
        )
        conn.execute(
            "INSERT INTO downloads (created_at, chat_id, user_id, user_name, platform, "
            "duration_sec, size_bytes, title, uploader, view_count) VALUES "
            "('2026-07-21T12:00:00+00:00', -100, 1, 'Максим', 'tiktok', 60, 1024, 'В', 'a', 5)"
        )

    result = StatsStore(path).weekly_stats(chat_id=-100, start=START, end=END)

    assert result.total_downloads == 1
    assert (result.top_users[0].name, result.top_users[0].username) == ("Максим", None)
