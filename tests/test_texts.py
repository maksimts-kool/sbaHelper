from datetime import UTC, datetime

import pytest

from sbahelper import texts
from sbahelper.checks import CheckResult
from sbahelper.cookies import CookieStatus
from sbahelper.download import VideoInfo
from sbahelper.stats import UserTally, aggregate

# NBSP, HANGUL FILLER and VARIATION SELECTOR-15: a name Telegram renders as blank.
INVISIBLE = "\xa0\u3164 \ufe0e \ufe0e\xa0"
WJ = "\u2060"


def info(**overrides) -> VideoInfo:
    fields = {"platform": "tiktok", "title": "Кот", "uploader": "catlover", "duration": 65}
    return VideoInfo(**{**fields, **overrides})


# --------------------------------------------------------------------------- #
#  Helpers                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "text"),
    [(999, "999"), (1_200, "1.2K"), (2_000_000, "2M"), (2_450_000, "2.5M"), (3 * 10**9, "3B")],
)
def test_counts_are_compact(value: int, text: str) -> None:
    assert texts.format_count(value) == text


def test_durations_and_sizes() -> None:
    assert texts.format_duration(65) == "1:05"
    assert texts.format_duration(-3) == "0:00"
    assert texts.format_total_duration(4320) == "1 ч 12 мин"
    assert texts.format_total_duration(3600) == "1 ч"
    assert texts.format_total_duration(180) == "3 мин"
    assert texts.format_total_duration(42) == "42 сек"
    assert texts.format_size(0) == "0 МБ"
    assert texts.format_size(int(2.5 * 1024**2)) == "2.5 МБ"
    assert texts.format_size(812 * 1024**2) == "812 МБ"
    assert texts.format_size(2 * 1024**3) == "2.0 ГБ"


def test_period_within_and_across_months() -> None:
    assert texts.format_period(datetime(2026, 7, 20), datetime(2026, 7, 26)) == "20–26 июля"
    assert texts.format_period(datetime(2026, 7, 29), datetime(2026, 8, 4)) == "29 июля – 4 августа"


@pytest.mark.parametrize(
    ("count", "word"),
    [(1, "день"), (2, "дня"), (4, "дня"), (5, "дней"), (11, "дней"), (21, "день"), (22, "дня")],
)
def test_plural(count: int, word: str) -> None:
    assert texts.plural(count, "день", "дня", "дней") == word


def test_bar_shows_the_share_with_at_least_one_block() -> None:
    assert texts.bar(47, 47) == "▓" * 10
    assert texts.bar(0, 47) == "░" * 10
    assert texts.bar(31, 47) == "▓" * 7 + "░" * 3
    assert texts.bar(1, 47) == "▓" + "░" * 9


def test_truncate_collapses_whitespace() -> None:
    assert texts.truncate("  много   пробелов ", 40) == "много пробелов"
    assert texts.truncate("а" * 20, 10) == "а" * 9 + "…"


def test_defuse_breaks_links_mentions_tags_and_times() -> None:
    assert texts.defuse("see https://x.io @bob #tag at 12:34") == (
        f"see h{WJ}ttps://x.io @{WJ}bob #{WJ}tag at 12:{WJ}34"
    )


def test_invisible_names_are_detected() -> None:
    assert not texts.has_visible_text(INVISIBLE)
    assert not texts.has_visible_text("\u2800\u200b")
    assert texts.has_visible_text(INVISIBLE + "я")


# --------------------------------------------------------------------------- #
#  Chat messages                                                              #
# --------------------------------------------------------------------------- #


def test_start_mentions_the_limits() -> None:
    text = texts.start(5, 50)
    assert "до 5 мин" in text and "до 50 МБ" in text


def test_caption_is_compact_and_skips_unknown_counts() -> None:
    caption = texts.caption(info(view_count=1_500))
    assert caption.startswith("🎬 <b>Кот</b>\n👤 catlover · ")
    assert f"1:{WJ}05" in caption
    assert "1.5K" in caption
    assert texts.LIKES not in caption


def test_caption_escapes_and_defuses_user_text() -> None:
    caption = texts.caption(info(title="<b>x</b> https://a.io", uploader="@creator & co"))
    assert f"&lt;b&gt;x&lt;/b&gt; h{WJ}ttps://a.io" in caption
    assert f"@{WJ}creator &amp; co" in caption


def test_status_and_progress() -> None:
    text = texts.status(info(title="Кот_1"), texts.progress(50))
    assert text.startswith("📹 <b>Кот_1</b>\n👤 catlover")
    assert "▓▓▓▓▓░░░░░  50%" in text
    assert texts.progress(150).endswith("100%")
    assert texts.progress(-5).endswith(" 0%")


def test_failure_reasons_are_escaped() -> None:
    assert texts.failed("<bad>") == "❌ Не получилось скачать.\n<code>&lt;bad&gt;</code>"


# --------------------------------------------------------------------------- #
#  Weekly stats                                                               #
# --------------------------------------------------------------------------- #

START, END = datetime(2026, 7, 20, tzinfo=UTC), datetime(2026, 7, 26, 20, tzinfo=UTC)


def row(user_id, name, platform="tiktok", views=None, title="Видео", username=None):
    return (user_id, name, username, platform, 60, 1024**2, title, "author", views)


def weekly(rows, **kwargs) -> str:
    return texts.weekly_stats(aggregate(rows, period_start=START, period_end=END), **kwargs)


def test_quiet_week() -> None:
    assert weekly([], title=texts.THIS_WEEK) == (
        "📊 <b>Эта неделя · 20–26 июля</b>\n\nПока тихо — ни одного видео."
    )


def test_full_summary() -> None:
    rows = [
        *[row(1, "Максим", views=2_400_000, title="Кот открывает холодильник")] * 4,
        *[row(2, "Аня", platform="youtube")] * 3,
        *[row(3, "Пётр", platform="youtube")] * 2,
        row(4, "Гость"),
    ]
    text = weekly(rows)

    assert text.startswith("📊 <b>Итоги недели · 20–26 июля</b>")
    assert "Скачано <b>10</b> видео · 10 мин · 10 МБ" in text
    assert "🥇 Максим — 4\n🥈 Аня — 3\n🥉 Пётр — 2\n   и ещё 1 участник — 1" in text
    assert "<blockquote>▓▓▓▓▓░░░░░  |  TikTok — 5\n▓▓▓▓▓░░░░░  |  YouTube — 5</blockquote>" in text
    assert text.endswith("🔥 <b>Хит недели</b>\n«Кот открывает холодильник» — 2.4M 👁")


def test_hit_of_the_week_needs_view_counts() -> None:
    assert "Хит недели" not in weekly([row(1, "Максим")])


def test_long_titles_are_truncated() -> None:
    text = weekly([row(1, "A", title="о" * 200, views=10)])
    assert "…" in text and "о" * 100 not in text


@pytest.mark.parametrize(
    ("tally", "label"),
    [
        (UserTally(1, "Максим", 1, "maksim"), "@maksim"),
        (UserTally(1, "@everyone", 1, "maksim"), "@maksim"),
        (UserTally(1, "@everyone", 1), f"@{WJ}everyone"),
        (UserTally(1, "<b>Хакер</b> & Ко", 1), "&lt;b&gt;Хакер&lt;/b&gt; &amp; Ко"),
        (UserTally(1083346705, INVISIBLE, 1), "Участник 1083346705"),
        (UserTally(0, INVISIBLE, 1), "Неизвестно"),
    ],
)
def test_user_labels(tally: UserTally, label: str) -> None:
    assert texts.user_label(tally) == label


def test_legacy_facebook_rows_still_render() -> None:
    assert "Facebook — 1" in weekly([row(1, "A", platform="facebook")])


# --------------------------------------------------------------------------- #
#  Admin tools                                                                #
# --------------------------------------------------------------------------- #


def test_cookie_status() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    text = texts.cookies_status(
        [
            CookieStatus("tiktok", 23, True, datetime(2026, 9, 11, tzinfo=UTC), now),
            CookieStatus("youtube", 0, False, None, None),
        ],
        now,
    )
    assert "• <b>TikTok</b> — 23 шт., вход выполнен, истекает 11 сентября (через 10 дней)" in text
    assert "• <b>YouTube</b> — нет" in text
    assert "Cookie-Editor" in text


def test_cookie_status_of_expired_and_anonymous_files() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    expired = CookieStatus("tiktok", 3, False, datetime(2026, 8, 1, tzinfo=UTC), now)
    anonymous = CookieStatus("youtube", 2, False, None, now)
    text = texts.cookies_status([expired, anonymous], now)
    assert "3 шт., вход истёк" in text
    assert "2 шт., без входа в аккаунт" in text


def test_cookies_saved_lists_platforms() -> None:
    assert "• TikTok — 2 шт.\n• YouTube — 1 шт." in texts.cookies_saved({"tiktok": 2, "youtube": 1})


def test_check_results() -> None:
    assert texts.check_results([]) == texts.CHECKS_NONE
    text = texts.check_results(
        [CheckResult("tiktok", True, "Кот (2.1 MB, 3 s)"), CheckResult("youtube", False, "<x>")]
    )
    assert "✅ <b>TikTok</b> — Кот (2.1 MB, 3 s)" in text
    assert "❌ <b>YouTube</b> — &lt;x&gt;" in text
