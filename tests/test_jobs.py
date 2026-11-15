import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError
from telegram.ext import ApplicationBuilder

from sbahelper import download, health, jobs
from sbahelper.config import settings
from sbahelper.cookies import CookieStatus
from sbahelper.db import Database
from sbahelper.download import LoginRequired
from sbahelper.prefs import Preferences
from sbahelper.runtime import RUNTIME_KEY, Runtime
from sbahelper.service import DownloadService
from sbahelper.stats import DownloadEvent, StatsStore

PERIOD = (datetime(2026, 7, 20, tzinfo=UTC), datetime(2026, 7, 27, tzinfo=UTC))
IN_PERIOD = datetime(2026, 7, 21, 12, tzinfo=UTC)


class FakeBot:
    def __init__(self, failing: dict[int, Exception] | None = None) -> None:
        self.sent: dict[int, str] = {}
        self.threads: dict[int, int | None] = {}
        self.failing = dict(failing or {})

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        error = self.failing.get(chat_id)
        if error and (not isinstance(error, BadRequest) or kwargs.get("message_thread_id")):
            raise error
        self.sent[chat_id] = text
        self.threads[chat_id] = kwargs.get("message_thread_id")


@pytest.fixture
def store(tmp_path: Path) -> StatsStore:
    return StatsStore(Database(tmp_path / "stats.db"))


def runtime_for(store: StatsStore | None) -> Runtime:
    db = store.db if store else None
    return Runtime(
        service=DownloadService(settings),
        db=db,
        stats=store,
        prefs=Preferences(db) if db else None,
    )


def record(store: StatsStore, chat_id: int, name: str, at: datetime = IN_PERIOD) -> None:
    store.record(DownloadEvent(chat_id, hash(name) % 10_000, name, None, "tiktok"), at=at)


def run_weekly(store: StatsStore | None, bot: FakeBot, monkeypatch) -> None:
    monkeypatch.setattr(jobs, "week_period", lambda now: PERIOD)
    context = SimpleNamespace(bot_data={RUNTIME_KEY: runtime_for(store)}, bot=bot)
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
    monkeypatch.setattr(settings, "access_policy", "allowlist")
    monkeypatch.setattr(settings, "allowed_chat_ids", frozenset({-100, -300}))
    bot = FakeBot()

    run_weekly(store, bot, monkeypatch)

    assert set(bot.sent) == {-300}


def test_one_failing_chat_does_not_stop_the_rest(store, monkeypatch) -> None:
    record(store, -100, "Максим")
    record(store, -200, "Аня")
    bot = FakeBot(failing={-100: TelegramError("Bad Gateway")})

    run_weekly(store, bot, monkeypatch)

    assert set(bot.sent) == {-200}


@pytest.mark.parametrize(("retention", "left"), [(30, 0), (0, 1)])
def test_old_rows_are_pruned_by_maintenance_unless_retention_is_zero(
    store, monkeypatch, retention, left
) -> None:
    record(store, -100, "Максим")
    record(store, -100, "Максим", at=datetime(2020, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(settings, "stats_retention_days", retention)

    context = SimpleNamespace(bot_data={RUNTIME_KEY: runtime_for(store)}, bot=FakeBot())
    asyncio.run(jobs.maintenance(context))

    old = store.weekly_stats(
        chat_id=-100, start=datetime(2019, 1, 1, tzinfo=UTC), end=datetime(2021, 1, 1, tzinfo=UTC)
    )
    assert old.total_downloads == left


def test_summary_goes_to_the_chosen_topic(store, monkeypatch) -> None:
    record(store, -100, "Максим")
    Preferences(store.db).update_chat(-100, summary_thread_id=77)
    bot = FakeBot()
    run_weekly(store, bot, monkeypatch)
    assert bot.threads[-100] == 77


def test_deleted_topic_falls_back_to_the_main_chat(store, monkeypatch) -> None:
    record(store, -100, "Максим")
    Preferences(store.db).update_chat(-100, summary_thread_id=77)
    bot = FakeBot(failing={-100: BadRequest("Message thread not found")})
    run_weekly(store, bot, monkeypatch)
    assert bot.threads[-100] is None
    assert Preferences(store.db).chat(-100).summary_thread_id is None


def test_kicked_bot_stops_sending_to_that_chat(store, monkeypatch) -> None:
    record(store, -100, "Максим")
    run_weekly(store, FakeBot(failing={-100: Forbidden("bot was kicked")}), monkeypatch)
    assert Preferences(store.db).chat(-100).active is False
    bot = FakeBot()
    run_weekly(store, bot, monkeypatch)
    assert bot.sent == {}


def test_migrated_group_gets_the_summary_at_its_new_id(store, monkeypatch) -> None:
    record(store, -100, "Максим")
    bot = FakeBot(failing={-100: ChatMigrated(-1001)})
    run_weekly(store, bot, monkeypatch)
    assert set(bot.sent) == {-1001}
    assert store.active_chat_ids(*PERIOD) == [-1001]


def test_maintenance_removes_only_old_abandoned_workdirs(monkeypatch) -> None:
    old = download.make_workdir()
    fresh = download.make_workdir()
    marker = old / download.WORKDIR_MARKER
    import os

    os.utime(marker, (0, 0))
    asyncio.run(jobs.maintenance(SimpleNamespace(bot_data={}, bot=FakeBot())))
    assert not old.exists() and fresh.exists()


def test_heartbeat_is_written() -> None:
    context = SimpleNamespace(bot_data={RUNTIME_KEY: runtime_for(None)})
    asyncio.run(jobs.heartbeat(context))
    assert health.check_heartbeat(settings.heartbeat_path)[0]


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


def test_cookie_refresh_visits_each_check_url_with_cookies(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        settings,
        "check_urls",
        {"tiktok": "https://vm.tiktok.com/x/", "youtube": "https://youtu.be/x"},
    )
    jobs.cookies.import_text(
        settings.cookies_dir, ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsid\n"
    )  # no TikTok cookies: nothing to keep alive there
    visited = []
    monkeypatch.setattr(jobs, "refresh", visited.append)

    with caplog.at_level(logging.INFO, logger="sbahelper.jobs"):
        jobs.refresh_cookies_now()

    assert visited == ["https://youtu.be/x"]
    assert [r.getMessage() for r in caplog.records] == ["Cookies refreshed. Platform=youtube"]


def test_rejected_cookies_during_refresh_alert_admins(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "check_urls", {"youtube": "https://youtu.be/x"})
    jobs.cookies.import_text(settings.cookies_dir, ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsid\n")

    def rejected(url):
        raise LoginRequired("Sign in to confirm you're not a bot")

    monkeypatch.setattr(jobs, "refresh", rejected)
    with caplog.at_level(logging.INFO, logger="sbahelper.jobs"):
        jobs.refresh_cookies_now()

    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "Send fresh cookies" in caplog.records[0].getMessage()


# --------------------------------------------------------------------------- #
#  Scheduling                                                                 #
# --------------------------------------------------------------------------- #


def scheduled(store: StatsStore | None) -> dict:
    app = ApplicationBuilder().token("123456:TEST").build()
    app.bot_data[RUNTIME_KEY] = runtime_for(store)
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
    always = {"cookie-expiry", "heartbeat", "maintenance"}
    assert set(scheduled(None)) == always
    monkeypatch.setattr(settings, "check_urls", {"tiktok": "https://vm.tiktok.com/x/"})
    assert set(scheduled(store)) == always | {"weekly-stats", "startup-checks", "cookie-refresh"}
    monkeypatch.setattr(settings, "cookies_refresh_hours", 0)
    assert "cookie-refresh" not in scheduled(store)


def test_cookie_refresh_runs_every_few_hours(monkeypatch) -> None:
    monkeypatch.setattr(settings, "check_urls", {"youtube": "https://youtu.be/x"})
    job = scheduled(None)["cookie-refresh"]
    assert job.job.trigger.interval == timedelta(hours=4)
