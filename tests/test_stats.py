"""Журнал загрузок и недельная агрегация — `downloader/stats.py`."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from downloader.stats import (
    DownloadEvent,
    StatsStore,
    aggregate_weekly_stats,
    empty_weekly_stats,
    resolve_timezone,
    sunday_based_weekday,
    week_period,
)

UTC = timezone.utc


def row(
    user_id: int,
    user_name: str,
    platform: str = "tiktok",
    duration_sec: int = 60,
    size_bytes: int = 1024 * 1024,
    title: str = "Видео",
    uploader: str = "author",
    view_count: int | None = None,
) -> tuple:
    return (user_id, user_name, platform, duration_sec, size_bytes, title, uploader, view_count)


class WeekPeriodTests(unittest.TestCase):
    def test_period_starts_on_monday_midnight(self):
        # Воскресенье 20:00.
        now = datetime(2026, 7, 26, 20, 0, tzinfo=UTC)
        start, end = week_period(now)
        self.assertEqual(start, datetime(2026, 7, 20, 0, 0, tzinfo=UTC))
        self.assertEqual(end, now)

    def test_monday_period_starts_the_same_day(self):
        now = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
        start, _ = week_period(now)
        self.assertEqual(start, datetime(2026, 7, 20, 0, 0, tzinfo=UTC))

    def test_sunday_based_weekday_matches_job_queue(self):
        # JobQueue считает 0 = воскресенье, datetime.weekday() — 0 = понедельник.
        self.assertEqual(sunday_based_weekday(6), 0)
        self.assertEqual(sunday_based_weekday(0), 1)
        self.assertEqual(sunday_based_weekday(5), 6)

    def test_unknown_timezone_falls_back_to_utc(self):
        self.assertEqual(resolve_timezone("Nowhere/Nothing"), UTC)


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 7, 20, tzinfo=UTC)
        self.end = datetime(2026, 7, 26, 20, 0, tzinfo=UTC)

    def aggregate(self, rows):
        return aggregate_weekly_stats(rows, period_start=self.start, period_end=self.end)

    def test_totals_and_ranking(self):
        rows = [
            *[row(1, "Максим", duration_sec=30, size_bytes=1_000_000)] * 3,
            *[row(2, "Аня", platform="youtube", duration_sec=60, size_bytes=2_000_000)] * 2,
            row(3, "Пётр", platform="other", duration_sec=90, size_bytes=3_000_000),
        ]
        stats = self.aggregate(rows)

        self.assertEqual(stats.total_downloads, 6)
        self.assertEqual(stats.total_duration_sec, 3 * 30 + 2 * 60 + 90)
        self.assertEqual(stats.total_size_bytes, 3_000_000 + 4_000_000 + 3_000_000)
        self.assertEqual(
            [(tally.name, tally.downloads) for tally in stats.top_users],
            [("Максим", 3), ("Аня", 2), ("Пётр", 1)],
        )
        self.assertEqual(stats.other_users, 0)
        self.assertEqual(
            [(tally.platform, tally.downloads) for tally in stats.platforms],
            [("tiktok", 3), ("youtube", 2), ("other", 1)],
        )

    def test_users_beyond_the_top_are_collapsed(self):
        rows = [
            *[row(1, "A")] * 5,
            *[row(2, "B")] * 4,
            *[row(3, "C")] * 3,
            *[row(4, "D")] * 2,
            row(5, "E"),
        ]
        stats = self.aggregate(rows)

        self.assertEqual([tally.name for tally in stats.top_users], ["A", "B", "C"])
        self.assertEqual(stats.other_users, 2)
        self.assertEqual(stats.other_downloads, 3)

    def test_latest_name_wins_for_the_same_user(self):
        stats = self.aggregate([row(1, "Старое имя"), row(1, "Новое имя")])
        self.assertEqual(stats.top_users[0].name, "Новое имя")

    def test_equal_counts_are_ordered_by_name(self):
        stats = self.aggregate([row(2, "Яна"), row(1, "Артём")])
        self.assertEqual([tally.name for tally in stats.top_users], ["Артём", "Яна"])

    def test_top_video_is_the_most_viewed(self):
        rows = [
            row(1, "A", title="Мало", view_count=10),
            row(1, "A", title="Много", view_count=2_400_000),
            row(2, "B", title="Без счётчика", view_count=None),
        ]
        stats = self.aggregate(rows)

        self.assertIsNotNone(stats.top_video)
        self.assertEqual(stats.top_video.title, "Много")
        self.assertEqual(stats.top_video.view_count, 2_400_000)

    def test_no_top_video_without_view_counts(self):
        stats = self.aggregate([row(1, "A", view_count=None)])
        self.assertIsNone(stats.top_video)

    def test_empty_period(self):
        stats = empty_weekly_stats(self.start, self.end)
        self.assertEqual(stats.total_downloads, 0)
        self.assertEqual(stats.top_users, ())
        self.assertEqual(stats.platforms, ())
        self.assertIsNone(stats.top_video)


class StatsStoreTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.path = str(Path(self._tempdir.name) / "nested" / "stats.db")
        self.store = StatsStore(self.path)
        self.start = datetime(2026, 7, 20, tzinfo=UTC)
        self.end = datetime(2026, 7, 27, tzinfo=UTC)

    def record(self, chat_id: int, user_id: int, name: str, at: datetime, **kwargs) -> None:
        event = DownloadEvent(
            chat_id=chat_id,
            user_id=user_id,
            user_name=name,
            platform="tiktok",
            **kwargs,
        )
        self.store.record(event, at=at)

    def test_creates_missing_directories(self):
        self.assertTrue(Path(self.path).exists())

    def test_reopening_keeps_existing_rows(self):
        self.record(-100, 1, "A", datetime(2026, 7, 21, 12, tzinfo=UTC))
        reopened = StatsStore(self.path)
        stats = reopened.weekly_stats(chat_id=-100, start=self.start, end=self.end)
        self.assertEqual(stats.total_downloads, 1)

    def test_stats_are_scoped_to_one_chat(self):
        self.record(-100, 1, "A", datetime(2026, 7, 21, 12, tzinfo=UTC))
        self.record(-100, 1, "A", datetime(2026, 7, 22, 12, tzinfo=UTC))
        self.record(-200, 2, "B", datetime(2026, 7, 22, 12, tzinfo=UTC))

        self.assertEqual(
            self.store.weekly_stats(chat_id=-100, start=self.start, end=self.end).total_downloads, 2
        )
        self.assertEqual(
            self.store.weekly_stats(chat_id=-200, start=self.start, end=self.end).total_downloads, 1
        )

    def test_rows_outside_the_period_are_ignored(self):
        self.record(-100, 1, "A", self.start - timedelta(seconds=1))
        self.record(-100, 1, "A", self.end)
        self.record(-100, 1, "A", self.start)

        stats = self.store.weekly_stats(chat_id=-100, start=self.start, end=self.end)
        self.assertEqual(stats.total_downloads, 1)

    def test_active_chat_ids_lists_only_chats_with_downloads(self):
        self.record(-100, 1, "A", datetime(2026, 7, 21, 12, tzinfo=UTC))
        self.record(-200, 2, "B", datetime(2026, 7, 22, 12, tzinfo=UTC))
        self.record(-300, 3, "C", self.start - timedelta(days=30))

        self.assertEqual(self.store.active_chat_ids(self.start, self.end), [-200, -100])

    def test_naive_timestamps_are_treated_as_utc(self):
        self.record(-100, 1, "A", datetime(2026, 7, 21, 12))
        stats = self.store.weekly_stats(chat_id=-100, start=self.start, end=self.end)
        self.assertEqual(stats.total_downloads, 1)

    def test_prune_removes_only_old_rows(self):
        self.record(-100, 1, "A", datetime(2026, 1, 1, tzinfo=UTC))
        self.record(-100, 1, "A", datetime(2026, 7, 21, tzinfo=UTC))

        removed = self.store.prune(older_than=datetime(2026, 6, 1, tzinfo=UTC))

        self.assertEqual(removed, 1)
        self.assertEqual(
            self.store.weekly_stats(chat_id=-100, start=self.start, end=self.end).total_downloads, 1
        )


if __name__ == "__main__":
    unittest.main()
