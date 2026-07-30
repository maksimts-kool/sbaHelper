"""Тексты для Telegram — `downloader/formatting.py`."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from downloader.formatting import (
    build_download_progress_line,
    build_platform_bar,
    build_status_text,
    build_video_caption,
    build_weekly_stats_message,
    escape_md_v2,
    format_count,
    format_duration,
    format_period,
    format_size,
    format_total_duration,
    plural_ru,
    prevent_auto_links,
    truncate,
)
from downloader.stats import aggregate_weekly_stats

UTC = timezone.utc


def stats_row(
    user_id: int,
    user_name: str,
    platform: str = "tiktok",
    duration_sec: int = 60,
    size_bytes: int = 1024 * 1024,
    title: str = "Видео",
    uploader: str = "author",
    view_count: int | None = None,
    username: str | None = None,
) -> tuple:
    return (
        user_id,
        user_name,
        username,
        platform,
        duration_sec,
        size_bytes,
        title,
        uploader,
        view_count,
    )


class CaptionTest(unittest.TestCase):
    @staticmethod
    def _info(**overrides) -> SimpleNamespace:
        fields = {
            "title": "Кот",
            "uploader": "catlover",
            "duration": 65,
            "view_count": 1_500,
            "like_count": None,
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def test_counts_are_compact(self) -> None:
        self.assertEqual(format_count(999), "999")
        self.assertEqual(format_count(1_200), "1.2K")
        self.assertEqual(format_count(2_000_000), "2M")
        self.assertEqual(format_count(None), "—")

    def test_duration_is_minutes_and_seconds(self) -> None:
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(0), "0:00")

    def test_caption_defuses_links_mentions_and_hashtags(self) -> None:
        caption = build_video_caption(
            self._info(title="Watch https://example.com #tag", uploader="@creator")
        )

        self.assertIn("1:⁠05", caption)
        self.assertIn("h⁠ttps://example.com", caption)
        self.assertIn("#⁠tag", caption)
        self.assertIn("@⁠creator", caption)

    def test_caption_escapes_html(self) -> None:
        caption = build_video_caption(self._info(title="<b>bold</b>", uploader="a & b"))

        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", caption)
        self.assertIn("a &amp; b", caption)

    def test_caption_shows_only_known_counts(self) -> None:
        caption = build_video_caption(self._info(view_count=1_500, like_count=None))

        self.assertIn("1.5K просмотров", caption)
        self.assertNotIn("лайков", caption)

    def test_caption_shows_likes_when_present(self) -> None:
        self.assertIn("310K лайков", build_video_caption(self._info(like_count=310_000)))


class ProgressAndStatusTest(unittest.TestCase):
    def test_progress_bar_is_clamped(self) -> None:
        self.assertIn("100%", build_download_progress_line(125))
        self.assertIn("0%", build_download_progress_line(-10))
        self.assertIn("▓▓▓▓▓░░░░░", build_download_progress_line(50))

    def test_markdown_v2_special_characters_are_escaped(self) -> None:
        self.assertEqual(escape_md_v2("a_b*c[d]"), r"a\_b\*c\[d\]")
        self.assertEqual(escape_md_v2("1.5 (max!)"), r"1\.5 \(max\!\)")

    def test_status_text_escapes_title_and_author(self) -> None:
        text = build_status_text("Кот_1", "author.name", " · 0:30", "статус")

        self.assertIn(r"Кот\_1", text)
        self.assertIn(r"author\.name", text)
        self.assertIn("статус", text)

    def test_prevent_auto_links_breaks_timestamps(self) -> None:
        self.assertEqual(prevent_auto_links("12:34"), "12:⁠34")


class StatsHelpersTest(unittest.TestCase):
    def test_plural_ru(self) -> None:
        cases = {1: "участник", 2: "участника", 4: "участника", 5: "участников", 11: "участников"}
        cases.update({12: "участников", 21: "участник", 22: "участника", 25: "участников"})
        for count, expected in cases.items():
            with self.subTest(count=count):
                self.assertEqual(plural_ru(count, "участник", "участника", "участников"), expected)

    def test_total_duration(self) -> None:
        self.assertEqual(format_total_duration(4320), "1 ч 12 мин")
        self.assertEqual(format_total_duration(3600), "1 ч")
        self.assertEqual(format_total_duration(180), "3 мин")
        self.assertEqual(format_total_duration(42), "42 сек")
        self.assertEqual(format_total_duration(-5), "0 сек")

    def test_total_size(self) -> None:
        self.assertEqual(format_size(0), "0 МБ")
        self.assertEqual(format_size(int(2.5 * 1024 * 1024)), "2.5 МБ")
        self.assertEqual(format_size(812 * 1024 * 1024), "812 МБ")
        self.assertEqual(format_size(2 * 1024 * 1024 * 1024), "2.0 ГБ")

    def test_period_within_and_across_months(self) -> None:
        self.assertEqual(format_period(datetime(2026, 7, 20), datetime(2026, 7, 26)), "20–26 июля")
        self.assertEqual(
            format_period(datetime(2026, 7, 29), datetime(2026, 8, 4)), "29 июля – 4 августа"
        )

    def test_platform_bar_shows_share_of_total(self) -> None:
        self.assertEqual(build_platform_bar(47, 47), "▓" * 10)
        self.assertEqual(build_platform_bar(0, 47), "░" * 10)
        self.assertEqual(build_platform_bar(31, 47), "▓" * 7 + "░" * 3)
        self.assertEqual(build_platform_bar(12, 47), "▓" * 3 + "░" * 7)
        # Даже одна загрузка из сорока семи рисуется как минимум одним блоком.
        self.assertEqual(build_platform_bar(1, 47), "▓" + "░" * 9)

    def test_truncate_collapses_whitespace(self) -> None:
        self.assertEqual(truncate("  много   пробелов  ", 40), "много пробелов")
        self.assertEqual(truncate("а" * 20, 10), "а" * 9 + "…")


class WeeklyMessageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 7, 20, tzinfo=UTC)
        self.end = datetime(2026, 7, 26, 20, 0, tzinfo=UTC)

    def build(self, rows, **kwargs) -> str:
        stats = aggregate_weekly_stats(rows, period_start=self.start, period_end=self.end)
        return build_weekly_stats_message(stats, **kwargs)

    def test_quiet_week(self) -> None:
        text = self.build([])

        self.assertIn("20–26 июля", text)
        self.assertIn("ни одного видео", text)

    def test_full_message(self) -> None:
        rows = [
            *[stats_row(1, "Максим", view_count=2_400_000, title="Кот открывает холодильник")] * 4,
            *[stats_row(2, "Аня", platform="youtube")] * 3,
            *[stats_row(3, "Пётр", platform="youtube")] * 2,
            stats_row(4, "Гость"),
        ]
        text = self.build(rows)

        self.assertIn("📊 <b>Итоги недели · 20–26 июля</b>", text)
        self.assertIn("Скачано: <b>10</b> видео", text)
        self.assertIn("🥇 Максим — 4", text)
        self.assertIn("🥈 Аня — 3", text)
        self.assertIn("🥉 Пётр — 2", text)
        self.assertIn("ещё 1 участник — 1", text)
        self.assertIn("<blockquote>", text)
        self.assertIn("▓▓▓▓▓░░░░░  |  TikTok — 5", text)
        self.assertIn("«Кот открывает холодильник» — 2.4M 👁", text)

    def test_hit_of_the_week_is_omitted_without_view_counts(self) -> None:
        self.assertNotIn("Хит недели", self.build([stats_row(1, "Максим")]))

    def test_long_video_title_is_truncated(self) -> None:
        text = self.build([stats_row(1, "A", title="о" * 200, view_count=10)])

        self.assertIn("…", text)
        self.assertNotIn("о" * 100, text)

    def test_custom_title(self) -> None:
        self.assertIn("Текущая неделя", self.build([], title="Текущая неделя"))

    def test_names_and_titles_are_escaped(self) -> None:
        text = self.build([stats_row(1, "<b>Хакер</b> & Ко", title="<script>", view_count=5)])

        self.assertNotIn("<b>Хакер</b>", text)
        self.assertIn("&lt;b&gt;Хакер&lt;/b&gt; &amp; Ко", text)
        self.assertIn("&lt;script&gt;", text)

    def test_mentions_do_not_ping(self) -> None:
        text = self.build([stats_row(1, "@everyone", view_count=1)])

        self.assertNotIn("@everyone", text)
        self.assertIn("@⁠everyone", text)

    def test_username_is_shown_as_a_real_mention(self) -> None:
        text = self.build([stats_row(1, "Максим", username="maksim")])

        self.assertIn("🥇 @maksim — 1", text)
        self.assertNotIn("Максим", text)

    def test_username_wins_over_a_name_that_looks_like_a_mention(self) -> None:
        # Подпись из аккаунта, а не то, чем человек назвался при загрузке.
        text = self.build([stats_row(1, "@everyone", username="maksim")])

        self.assertIn("@maksim", text)
        self.assertNotIn("everyone", text)

    def test_latest_known_username_is_used(self) -> None:
        rows = [stats_row(1, "Максим"), stats_row(1, "Максим", username="maksim")]

        self.assertIn("@maksim", self.build(rows))
        # И наоборот: пропавший в последней строке username не теряется.
        self.assertIn("@maksim", self.build(list(reversed(rows))))

    def test_legacy_facebook_rows_still_render(self) -> None:
        # Facebook больше не поддерживается, но старые записи должны читаться.
        text = self.build([stats_row(1, "A", platform="facebook")])

        self.assertIn("Facebook", text)


if __name__ == "__main__":
    unittest.main()
