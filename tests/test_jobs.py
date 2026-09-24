import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder

from sbahelper import jobs
from sbahelper.config import settings
from sbahelper.cookies import CookieStatus
from sbahelper.handlers import STATS_KEY
from sbahelper.stats import DownloadEvent, StatsStore

PERIOD = (datetime(2026, 7, 20, tzinfo=UTC), datetime(2026, 7, 27, tzinfo=UTC))
IN_PERIOD = datetime(2026, 7, 21, 12, tzinfo=UTC)


class FakeBot:
    def __init__(self, failing: set[int] = frozenset()) -> None:
        self.sent: dict[int, str] = {}
        self.failing = failing

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        if chat_id in self.failing:
            raise TelegramError("Forbidden: bot was kicked from the group chat")
        self.sent[chat_id] = text


@pytest.fixture
def store(tmp_path: Path) -> StatsStore:
    return StatsStore(tmp_path / "stats.db")


def record(store: StatsStore, chat_id: int, name: str, at: datetime = IN_PERIOD) -> None:
    store.record(DownloadEvent(chat_id, hash(name) % 10_000, name, None, "tiktok"), at=at)


def run_weekly(store: StatsStore | None, bot: FakeBot, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "week_period", lambda now: PERIOD)
    context = SimpleNamespace(bot_data={STATS_KEY: store}, bot=bot)
    asyncio.run(jobs.send_weekly_stats(context))


# --------------------------------------------------------------------------- #
#  Weekly summary                                                             #
# --------------------------------------------------------------------------- #


def test_each_chat_gets_only_its_own_numbers(store, monkeypatch) -> None:
    record(store, -100, "Максим")
    record(store, -100, "Максим")
    record(store, -200, "Аня")
    bot = FakeBot()

    run_weekly(store, bot, monkeypatch)

    assert set(bot.sent) == {-100, -200}
    assert "Максим — 2" in bot.sent[-100]
    assert "Максим" not in bot.sent[-200]


def test_quiet_and_disallowed_chats_get_nothing(store, monkeypatch) -> None:
    record(store, -100, "Максим", at=datetime(2026, 7, 10, tzinfo=UTC))  # last week
    record(store, -200, "Аня")
    record(store, -300, "Пётр")
    monkeypatch.setattr(settings, "allowed_chat_ids", frozenset({-100, -300}))
    bot = FakeBot()

    run_weekly(store, bot, monkeypatch)

    assert set(bot.sent) == {-300}


def test_one_failing_chat_does_not_stop_the_rest(store, monkeypatch) -> None:
    record(store, -100, "Максим")
    record(store, -200, "Аня")
    bot = FakeBot(failing={-100})

    run_weekly(store, bot, monkeypatch)

    assert set(bot.sent) == {-200}


@pytest.mark.parametrize(("retention", "left"), [(30, 0), (0, 1)])
def test_old_rows_are_pruned_unless_retention_is_zero(store, monkeypatch, retention, left) -> None:
    record(store, -100, "Максим")
    record(store, -100, "Максим", at=datetime(2020, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(settings, "stats_retention_days", retention)

    run_weekly(store, FakeBot(), monkeypatch)

    old = store.weekly_stats(
        chat_id=-100, start=datetime(2019, 1, 1, tzinfo=UTC), end=datetime(2021, 1, 1, tzinfo=UTC)
    )
    assert old.total_downloads == left


def test_no_store_means_no_summary(monkeypatch) -> None:
    bot = FakeBot()
    run_weekly(None, bot, monkeypatch)
    assert bot.sent == {}


# --------------------------------------------------------------------------- #
#  Cookie reminder                                                            #
# --------------------------------------------------------------------------- #

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def cookie_status(platform: str, logged_in: bool, expires_in_days: float | None) -> CookieStatus:
    expires = NOW + timedelta(days=expires_in_days) if expires_in_days is not None else None
    return CookieStatus(platform, 5, logged_in, expires, NOW)


def test_only_expiring_or_expired_logins_are_problems() -> None:
    problems = jobs.cookie_problems(
        [
            cookie_status("tiktok", True, 2),
            cookie_status("youtube", False, -1),
            cookie_status("tiktok", True, 30),
            cookie_status("youtube", False, None),  # anonymous cookies: nothing to expire
        ],
        NOW,
    )
    assert problems == [
        "tiktok login expires 2026-09-03 00:00 UTC",
        "youtube login expired 2026-08-31",
    ]


def test_reminder_is_a_single_error(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        jobs.cookies, "status", lambda directory: [cookie_status("tiktok", True, 0.5)]
    )
    with caplog.at_level(logging.INFO):
        asyncio.run(jobs.check_cookie_expiry(SimpleNamespace()))
    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "Send fresh cookies" in caplog.records[0].getMessage()


# --------------------------------------------------------------------------- #
#  Scheduling                                                                 #
# --------------------------------------------------------------------------- #


def scheduled(store: StatsStore | None) -> dict:
    app = ApplicationBuilder().token("123456:TEST").build()
    app.bot_data[STATS_KEY] = store
    jobs.schedule(app)
    return {job.name: job for job in app.job_queue.jobs()}


def test_weekly_summary_fires_on_the_configured_day_and_time(store) -> None:
    job = scheduled(store)["weekly-stats"]

    wednesday = datetime(2026, 7, 29, 12, tzinfo=settings.tz)
    next_run = job.job.trigger.get_next_fire_time(None, wednesday)

    assert next_run.weekday() == 6
    assert (next_run.hour, next_run.minute) == (20, 0)
    assert next_run.utcoffset() == settings.tz.utcoffset(next_run)


def test_jobs_depend_on_configuration(store, monkeypatch) -> None:
    assert set(scheduled(None)) == {"cookie-expiry"}
    monkeypatch.setattr(settings, "check_urls", {"tiktok": "https://vm.tiktok.com/x/"})
    assert set(scheduled(store)) == {"weekly-stats", "cookie-expiry", "startup-checks"}
