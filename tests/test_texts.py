from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sbahelper import texts
from sbahelper.checks import CheckResult
from sbahelper.cookies import CookieStatus, Health
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
    # Decimal units, the same as MAX_FILE_SIZE_BYTES: 1 МБ = 1,000,000 bytes.
    assert texts.format_size(0) == "0 МБ"
    assert texts.format_size(2_500_000) == "2.5 МБ"
    assert texts.format_size(45_000_000) == "45 МБ"
    assert texts.format_size(812_000_000) == "812 МБ"
    assert texts.format_size(2_000_000_000) == "2.0 ГБ"


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
    text = texts.start(5, 45_000_000)
    assert "до 5 мин" in text and "до 45 МБ" in text


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
    return (user_id, name, username, platform, 60, 1_000_000, title, "author", views)


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


def test_cookie_status_shows_what_the_site_said_last_time() -> None:
    now = datetime(2026, 9, 27, 15, tzinfo=ZoneInfo("Europe/Tallinn"))
    expires = datetime(2027, 10, 23, tzinfo=UTC)
    rejected = Health(False, datetime(2026, 9, 27, 11, 2, tzinfo=UTC), "Sign in")
    accepted = Health(True, datetime(2026, 9, 26, 20, 30, tzinfo=UTC))
    text = texts.cookies_status(
        [
            CookieStatus("tiktok", 24, True, expires, now, accepted),
            CookieStatus("youtube", 23, True, expires, now, rejected),
        ],
        now,
    )
    assert "✅ работают, последний раз 26 сентября в 23:30" in text
    assert "❌ сайт просит войти заново (27 сентября в 14:02)" in text
    assert "инкогнито" in text


def test_cookie_status_without_a_result_suggests_a_check() -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    text = texts.cookies_status([CookieStatus("tiktok", 3, True, None, now)], now)
    assert "❔ ещё не проверялись — /check" in text


def test_cookies_saved_lists_platforms() -> None:
    assert "• TikTok — 2 шт.\n• YouTube — 1 шт." in texts.cookies_saved({"tiktok": 2, "youtube": 1})


def test_check_results() -> None:
    assert texts.check_results([]) == texts.CHECKS_NONE
    text = texts.check_results(
        [CheckResult("tiktok", True, "Кот (2.1 MB, 3 s)"), CheckResult("youtube", False, "<x>")]
    )
    assert "✅ <b>TikTok</b> — Кот (2.1 MB, 3 s)" in text
    assert "❌ <b>YouTube</b> — &lt;x&gt;" in text


# --------------------------------------------------------------------------- #
#  Telegram limits                                                            #
# --------------------------------------------------------------------------- #

EMOJI_FAMILY = "👨\u200d👩\u200d👧\u200d👦"


def test_visible_length_counts_utf16_after_parsing() -> None:
    assert texts.visible_length("<b>a&amp;b</b>") == 3
    assert texts.visible_length("😀") == 2  # one code point, two UTF-16 units
    assert texts.visible_length(texts.INFO) == 2  # custom emoji tag → its fallback


@pytest.mark.parametrize(
    "title",
    [
        "о" * 5000,
        "&<>" * 2000,
        "😀" * 3000,
        EMOJI_FAMILY * 500,
        "a\u0301" * 3000,  # combining accents
        "#tag @user https://x.io 12:34 " * 300,
    ],
)
def test_caption_and_status_fit_telegram_limits(title: str) -> None:
    video = info(title=title, uploader=title, view_count=10**9, like_count=10**9)
    caption = texts.caption(video)
    status = texts.status(video, texts.progress(50))
    assert texts.visible_length(caption) <= texts.CAPTION_LIMIT
    assert texts.visible_length(status) <= texts.MESSAGE_LIMIT
    # Escapes are never cut: every "&" starts a complete entity.
    import re

    assert not re.search(r"&(?!amp;|lt;|gt;|quot;|#x27;)", caption)
    assert caption.count("<b>") == caption.count("</b>") == 1


def test_truncate_never_ends_inside_a_joined_emoji() -> None:
    cut = texts.truncate("x" + EMOJI_FAMILY * 3, 4)
    assert not cut.rstrip("…").endswith("\u200d")


def test_fit_shortens_the_longest_field() -> None:
    def render(fields: dict[str, str]) -> str:
        return f"<b>{texts.safe(fields['a'])}</b> {texts.safe(fields['b'])}"

    text = texts.fit(render, {"a": "x" * 50, "b": "short"}, 20)
    assert texts.visible_length(text) <= 20 and "short" in text


def test_weekly_summary_with_long_names_fits() -> None:
    rows = [row(i, "Ж" * 500 + str(i), title="Т" * 5000, views=i) for i in range(1, 50)]
    assert texts.visible_length(weekly(rows)) <= texts.MESSAGE_LIMIT


def test_error_texts_by_code() -> None:
    from sbahelper import download

    assert texts.error_text(download.Rejected("x", code="photo"), 45_000_000).startswith("🖼")
    too_big = download.Rejected("x", code="too_big", size=52_000_000)
    assert "52 МБ" in texts.error_text(too_big, 45_000_000)
    assert "45 МБ" in texts.error_text(too_big, 45_000_000)
    too_long = download.Rejected("x", code="too_long", duration=400, limit=300)
    assert "6:40" in texts.error_text(too_long, 1) and "5 мин" in texts.error_text(too_long, 1)
    login = texts.error_text(download.LoginRequired("Sign in"), 1)
    assert "Возможно" in login  # not presented as a certain diagnosis
    for code in sorted(texts.REFUSAL_CODES):
        assert texts.refusal(code) != texts.UNEXPECTED


def test_failure_reason_is_clipped_before_escaping() -> None:
    text = texts.failed("<" * 1000)
    assert text.endswith("…</code>") and "&lt" + "</code>" not in text


def test_period_titles_and_quiet_day() -> None:
    from sbahelper.stats import aggregate as agg

    day = agg([], period_start=datetime(2026, 7, 21), period_end=datetime(2026, 7, 21, 15))
    assert texts.period_stats(day, period="day").startswith("📊 <b>Сегодня · 21 июля</b>")


def test_anonymous_rows_are_counted_without_names() -> None:
    text = weekly([row(1, "Максим"), row(0, "", views=None)])
    assert "Скачано <b>2</b>" in text and "без имени — 1" in text


def test_privacy_text_states_retention() -> None:
    text = texts.privacy(retention_days=400, cache_days=30)
    assert "400 дней" in text and "до 30 дней" in text
    assert "журналы" in text.lower()
    assert "бессрочно" in texts.privacy(retention_days=0, cache_days=0)


def test_cookie_confirmation_admits_a_failed_deletion() -> None:
    assert "удали его сам" in texts.cookies_saved({"tiktok": 1}, deleted=False)
    assert "Сообщение с файлом удалил" in texts.cookies_saved({"tiktok": 1})
