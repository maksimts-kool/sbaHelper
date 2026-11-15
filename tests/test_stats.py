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


# --------------------------------------------------------------------------- #
#  Periods, opt-out, deletion, migration                                      #
# --------------------------------------------------------------------------- #

from zoneinfo import ZoneInfo  # noqa: E402

from sbahelper.prefs import Preferences  # noqa: E402
from sbahelper.stats import period_bounds  # noqa: E402

TALLINN = ZoneInfo("Europe/Tallinn")


def test_period_bounds_are_calendar_periods() -> None:
    now = datetime(2026, 7, 22, 15, 30, tzinfo=TALLINN)  # a Wednesday
    assert period_bounds("day", now)[0] == datetime(2026, 7, 22, tzinfo=TALLINN)
    assert period_bounds("week", now)[0] == datetime(2026, 7, 20, tzinfo=TALLINN)
    assert period_bounds("month", now)[0] == datetime(2026, 7, 1, tzinfo=TALLINN)
    with pytest.raises(ValueError):
        period_bounds("year", now)


def test_week_across_dst_starts_at_local_midnight() -> None:
    # Clocks go back on Sunday 2026-10-25 in Tallinn (+03:00 → +02:00).
    sunday = datetime(2026, 10, 25, 20, tzinfo=TALLINN)
    start, _ = period_bounds("week", sunday)
    assert (start.hour, start.minute) == (0, 0)
    assert start.utcoffset() == timedelta(hours=3)  # Monday was still summer time
    assert sunday.utcoffset() == timedelta(hours=2)


def test_month_across_dst_starts_at_local_midnight() -> None:
    november = datetime(2026, 11, 3, 12, tzinfo=TALLINN)
    start, _ = period_bounds("month", november)
    assert start == datetime(2026, 11, 1, tzinfo=TALLINN) and start.utcoffset() == timedelta(
        hours=2
    )


def test_adjacent_periods_do_not_double_count(tmp_path: Path) -> None:
    store = StatsStore(tmp_path / "s.db")
    boundary = datetime(2026, 7, 27, tzinfo=TALLINN)  # Monday 00:00
    store.record(DownloadEvent(-1, 1, "A", None, "tiktok"), at=boundary)
    last_week = store.period_stats(chat_id=-1, start=boundary - timedelta(days=7), end=boundary)
    this_week = store.period_stats(chat_id=-1, start=boundary, end=boundary + timedelta(days=7))
    assert (last_week.total_downloads, this_week.total_downloads) == (0, 1)


def test_opted_out_users_are_stored_without_identity(tmp_path: Path) -> None:
    store = StatsStore(tmp_path / "s.db")
    Preferences(store.db).set_stats_opt_out(7, True)
    store.record(DownloadEvent(-1, 7, "Секрет", "secret", "tiktok"), at=START + timedelta(hours=1))

    week = store.period_stats(chat_id=-1, start=START, end=END)

    assert week.total_downloads == 1 and week.top_users == () and week.anonymous_downloads == 1
    with closing(sqlite3.connect(tmp_path / "s.db")) as conn:
        assert conn.execute("SELECT user_id, user_name, username FROM downloads").fetchall() == [
            (0, "", None)
        ]


def test_delete_user_removes_only_their_rows_everywhere(tmp_path: Path) -> None:
    store = StatsStore(tmp_path / "s.db")
    at = START + timedelta(hours=1)
    for chat in (-1, -2):
        store.record(DownloadEvent(chat, 7, "Я", None, "tiktok"), at=at)
        store.record(DownloadEvent(chat, 8, "Другой", None, "tiktok"), at=at)
    Preferences(store.db).set_stats_opt_out(7, True)

    assert store.user_row_count(7) == 2
    assert store.delete_user(7) == 2

    assert store.user_row_count(7) == 0 and store.user_row_count(8) == 2
    assert Preferences(store.db).user(7).stats_opt_out is False  # settings gone too
    week = store.period_stats(chat_id=-1, start=START, end=END)
    assert [t.user_id for t in week.top_users] == [8] and week.total_downloads == 1


def test_group_migration_moves_history_and_settings(tmp_path: Path) -> None:
    store = StatsStore(tmp_path / "s.db")
    prefs = Preferences(store.db)
    store.record(DownloadEvent(-1, 1, "A", None, "tiktok"), at=START + timedelta(hours=1))
    prefs.update_chat(-1, auto_download=False)

    assert store.migrate_chat(-1, -1001) == 1

    assert store.period_stats(chat_id=-1001, start=START, end=END).total_downloads == 1
    assert prefs.chat(-1001).auto_download is False
    assert prefs.chat(-1).auto_download is True  # back to defaults


def test_inactive_chats_get_no_summary(tmp_path: Path) -> None:
    store = StatsStore(tmp_path / "s.db")
    at = START + timedelta(hours=1)
    store.record(DownloadEvent(-1, 1, "A", None, "tiktok"), at=at)
    store.record(DownloadEvent(-2, 1, "A", None, "tiktok"), at=at)
    Preferences(store.db).update_chat(-2, active=False)
    assert store.active_chat_ids(START, END) == [-1]
