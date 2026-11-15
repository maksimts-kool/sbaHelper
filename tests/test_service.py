"""Download orchestration: admission, fair queue, dedup, cache, delivery and shutdown."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from telegram.error import BadRequest, Forbidden, TelegramError, TimedOut

from sbahelper import download, service, texts
from sbahelper.cache import FileCache
from sbahelper.config import settings
from sbahelper.db import Database
from sbahelper.download import Rejected
from sbahelper.links import parse_link
from sbahelper.prefs import Preferences
from sbahelper.service import DownloadService, Refused, Request, StatusMessage
from sbahelper.stats import StatsStore
from tests.fakes import INFO, FakeMessage, FakeRunner, drain

URL = "https://www.tiktok.com/@cat/video/7234567890123"


@pytest.fixture(autouse=True)
def fast(monkeypatch) -> None:
    monkeypatch.setattr(service, "MIN_EDIT_INTERVAL_SEC", 0)
    monkeypatch.setattr(service, "REJECTION_TTL_SEC", 0)
    monkeypatch.setattr(service, "CHAT_ACTION_EVERY_SEC", 0.01)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "bot.db")


def make_service(db: Database | None = None, runner: FakeRunner | None = None, **kwargs):
    runner = runner or FakeRunner()
    cache = stats = prefs = None
    if db is not None:
        cache = FileCache(db, bot_id=1, policy=1, ttl_days=30)
        stats = StatsStore(db)
        prefs = Preferences(db)
    svc = DownloadService(settings, cache=cache, stats=stats, prefs=prefs, runner=runner, **kwargs)
    return svc, runner


async def submit(svc: DownloadService, url: str = URL, *, chat_id=-100, user_id=42) -> FakeMessage:
    message = FakeMessage(url, chat_id=chat_id)
    ticket = svc.admit(chat_id, user_id)
    status = await message.reply_text(texts.CHECKING)
    link = parse_link(url)
    assert link is not None
    request = Request(link, message, StatusMessage(status), ticket, chat_id, user_id, "Максим")
    await svc.submit(request)
    return message


def week_total(db: Database, chat_id: int = -100) -> int:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    stats = StatsStore(db).period_stats(
        chat_id=chat_id, start=now - timedelta(days=1), end=now + timedelta(days=1)
    )
    return stats.total_downloads


# --------------------------------------------------------------------------- #
#  Delivery and cache                                                         #
# --------------------------------------------------------------------------- #


def test_download_is_sent_cached_recorded_and_cleaned(db) -> None:
    async def scenario():
        svc, runner = make_service(db)
        message = await submit(svc)
        await drain(svc)
        return svc, runner, message

    svc, runner, message = asyncio.run(scenario())

    kind, payload, kwargs = message.media[0]
    assert (kind, payload) == ("video", b"video")
    assert kwargs["caption"] == texts.caption(INFO)
    assert (kwargs["width"], kwargs["height"], kwargs["duration"]) == (576, 1024, 14)
    assert message.status.deleted
    assert not runner.workdirs[0].exists()
    assert svc.cache.get("tiktok:7234567890123").file_id == "FILE-1"
    assert week_total(db) == 1
    assert svc._pending_user == {} and svc._pending_chat == {}


def test_cached_video_is_sent_by_file_id_without_downloading(db) -> None:
    async def scenario():
        svc, runner = make_service(db)
        await submit(svc)
        await drain(svc)
        second = await submit(svc, chat_id=-200)
        await drain(svc)
        return svc, runner, second

    svc, runner, second = asyncio.run(scenario())
    assert len(runner.calls) == 1
    assert second.media[0][:2] == ("video", "FILE-1")
    assert svc.cache_hits == 1
    assert week_total(db, -200) == 1  # cached deliveries count too


def test_stale_file_id_is_invalidated_and_downloaded_again(db) -> None:
    async def scenario():
        svc, runner = make_service(db)
        svc.cache.put("tiktok:7234567890123", "OLD", "video", INFO, 5)
        message = FakeMessage(URL)
        message.video_errors = [BadRequest("Wrong file identifier/http url specified")]
        ticket = svc.admit(-100, 42)
        status = StatusMessage(await message.reply_text(texts.CHECKING))
        await svc.submit(Request(parse_link(URL), message, status, ticket, -100, 42))
        await drain(svc)
        return svc, runner, message

    svc, runner, message = asyncio.run(scenario())
    assert svc.cache_invalidated == 1 and len(runner.calls) == 1
    assert message.media[0][:2] == ("video", b"video")
    assert svc.cache.get("tiktok:7234567890123").file_id == "FILE-1"


def test_concurrent_requests_share_one_download(db) -> None:
    async def scenario():
        svc, runner = make_service(db)
        gate = runner.block()
        first = await submit(svc, chat_id=-100, user_id=1)
        await asyncio.to_thread(runner.started.wait, 5)
        second = await submit(svc, chat_id=-200, user_id=2)
        gate.set()
        await drain(svc)
        return runner, first, second

    runner, first, second = asyncio.run(scenario())
    assert len(runner.calls) == 1
    assert first.media[0][1] == b"video"  # uploaded once…
    assert second.media[0][1] == "FILE-1"  # …then reused by file_id
    assert not runner.workdirs[0].exists()


def test_failed_first_upload_does_not_poison_other_waiters(db) -> None:
    async def scenario():
        svc, runner = make_service(db)
        gate = runner.block()
        first = await submit(svc, chat_id=-100, user_id=1)
        first.video_errors = [TelegramError("Request Entity Too Large")]
        await asyncio.to_thread(runner.started.wait, 5)
        second = await submit(svc, chat_id=-200, user_id=2)
        gate.set()
        await drain(svc)
        return svc, runner, first, second

    svc, runner, first, second = asyncio.run(scenario())
    assert first.media == [] and "Request Entity Too Large" in first.status.edits[-1]
    assert second.media[0][1] == b"video"  # uploaded again from the same file
    assert not runner.workdirs[0].exists()
    assert svc.cache.get("tiktok:7234567890123") is not None


def test_short_link_resolving_to_a_cached_video_skips_the_download(db) -> None:
    async def scenario():
        svc, runner = make_service(db)
        svc.cache.put("tiktok:7234567890123", "CACHED", "video", INFO, 5)

        def resolving_runner(url, *, on_info=None, on_progress=None, cancel=None):
            runner.calls.append(url)
            on_info(INFO)  # raises Satisfied: the resolved id is cached
            raise AssertionError("should have stopped")

        svc._runner = resolving_runner
        message = await submit(svc, "https://vm.tiktok.com/ZSabc/")
        await drain(svc)
        return svc, message

    svc, message = asyncio.run(scenario())
    assert message.media[0][1] == "CACHED"
    assert svc.cache.resolve("url:vm.tiktok.com/ZSabc") == "tiktok:7234567890123"


def test_upload_timeout_is_not_retried(db) -> None:
    async def scenario():
        svc, _ = make_service(db)
        message = FakeMessage(URL)
        message.video_errors = [TimedOut()]
        ticket = svc.admit(-100, 42)
        status = StatusMessage(await message.reply_text(texts.CHECKING))
        await svc.submit(Request(parse_link(URL), message, status, ticket, -100, 42))
        await drain(svc)
        return message

    message = asyncio.run(scenario())
    assert message.media == [] and message.status.edits[-1] == texts.UPLOAD_UNCERTAIN


def test_blocked_bot_marks_the_chat_inactive(db) -> None:
    async def scenario():
        svc, _ = make_service(db)
        message = FakeMessage(URL)
        message.video_errors = [Forbidden("bot was kicked from the group chat")]
        ticket = svc.admit(-100, 42)
        status = StatusMessage(await message.reply_text(texts.CHECKING))
        await svc.submit(Request(parse_link(URL), message, status, ticket, -100, 42))
        await drain(svc)

    asyncio.run(scenario())
    assert Preferences(db).chat(-100).active is False


# --------------------------------------------------------------------------- #
#  Failures                                                                   #
# --------------------------------------------------------------------------- #


def test_rejection_is_flashed_then_removed() -> None:
    async def scenario():
        svc, _ = make_service(runner=FakeRunner(error=Rejected("photo post", code="photo")))
        message = await submit(svc)
        await drain(svc)
        return svc, message

    svc, message = asyncio.run(scenario())
    assert message.status.edits[-1] == texts.refusal("photo") or message.status.edits[
        -1
    ].startswith("🖼")
    assert message.status.deleted and message.media == []
    assert svc._pending_user == {}


def test_failure_stays_in_chat_and_is_a_warning(caplog) -> None:
    async def scenario():
        svc, _ = make_service(runner=FakeRunner(error=download.Unavailable("Video unavailable")))
        message = await submit(svc)
        await drain(svc)
        return message

    message = asyncio.run(scenario())
    assert message.status.edits[-1].startswith("🚫") and not message.status.deleted
    assert [r.levelname for r in caplog.records if "Download failed" in r.getMessage()] == [
        "WARNING"
    ]


def test_login_wall_alerts_the_admins(caplog) -> None:
    async def scenario():
        svc, _ = make_service(runner=FakeRunner(error=download.LoginRequired("Sign in")))
        await submit(svc)
        await drain(svc)

    asyncio.run(scenario())
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert "Send fresh cookies" in errors[0].getMessage()


def test_crash_shows_a_generic_error() -> None:
    async def scenario():
        svc, _ = make_service(runner=FakeRunner(error=KeyError("x")))
        message = await submit(svc)
        await drain(svc)
        return message

    assert asyncio.run(scenario()).status.edits[-1] == texts.UNEXPECTED


def test_overrun_reports_timeout_but_keeps_the_slot_until_the_thread_ends(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "download_timeout_sec", 0)
    monkeypatch.setattr(service, "OVERRUN_GRACE_SEC", 0.05)

    class Stubborn(FakeRunner):
        def __call__(self, url, *, on_info=None, on_progress=None, cancel=None):
            self.calls.append(url)
            time.sleep(0.3)  # ignores cancellation for a while
            return super().__call__(url)

    runner = Stubborn()

    async def scenario():
        svc, _ = make_service(runner=runner)
        message = await submit(svc)
        await asyncio.sleep(0.15)
        held = svc.active
        await drain(svc)
        return svc, message, held

    svc, message, held = asyncio.run(scenario())
    assert message.status.edits[-1].startswith("⌛")
    assert held == 1 and svc.active == 0
    assert not runner.workdirs[0].exists()  # the late result was cleaned up


# --------------------------------------------------------------------------- #
#  Admission and fairness                                                     #
# --------------------------------------------------------------------------- #


def test_per_user_and_per_chat_limits(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_pending_per_user", 1)
    monkeypatch.setattr(settings, "max_pending_per_chat", 2)
    svc, _ = make_service()
    ticket = svc.admit(-100, 1)
    with pytest.raises(Refused, match="user_busy"):
        svc.admit(-100, 1)
    svc.admit(-100, 2)
    with pytest.raises(Refused, match="chat_busy"):
        svc.admit(-100, 3)
    ticket.release()
    ticket.release()  # idempotent
    svc.admit(-100, 1)


def test_rate_limit_per_user(monkeypatch) -> None:
    monkeypatch.setattr(settings, "user_links_per_minute", 2)
    now = [0.0]
    svc, _ = make_service(clock=lambda: now[0])
    svc.admit(-1, 1).release()
    svc.admit(-2, 1).release()
    with pytest.raises(Refused, match="rate_limited"):
        svc.admit(-3, 1)
    now[0] = 61.0
    svc.admit(-3, 1)


def test_low_disk_space_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(service, "free_bytes", lambda root=None: 1_000)
    svc, _ = make_service()
    with pytest.raises(Refused, match="no_space"):
        svc.admit(-1, 1)


def test_queue_saturation(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_active_downloads", 1)
    monkeypatch.setattr(settings, "max_queued_downloads", 1)

    async def scenario():
        svc, runner = make_service()
        gate = runner.block()
        await submit(svc, "https://www.tiktok.com/@a/video/1", user_id=1)
        await asyncio.to_thread(runner.started.wait, 5)
        queued = await submit(svc, "https://www.tiktok.com/@a/video/2", user_id=2)
        with pytest.raises(Refused, match="busy"):
            svc.admit(-100, 3)
        gate.set()
        await drain(svc)
        return queued

    queued = asyncio.run(scenario())
    assert texts.QUEUED in queued.status.edits  # one notice, not a running position count


def test_chats_are_served_round_robin(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_active_downloads", 1)
    monkeypatch.setattr(settings, "max_pending_per_user", 10)

    async def scenario():
        svc, runner = make_service()
        gate = runner.block()
        await submit(svc, "https://www.tiktok.com/@a/video/11", chat_id=-1, user_id=1)
        await asyncio.to_thread(runner.started.wait, 5)
        await submit(svc, "https://www.tiktok.com/@a/video/12", chat_id=-1, user_id=1)
        await submit(svc, "https://www.tiktok.com/@a/video/13", chat_id=-1, user_id=1)
        await submit(svc, "https://www.tiktok.com/@a/video/21", chat_id=-2, user_id=2)
        gate.set()
        await drain(svc)
        return runner

    runner = asyncio.run(scenario())
    order = [url.rsplit("/", 1)[1] for url in runner.calls]
    assert order == ["11", "12", "21", "13"]


# --------------------------------------------------------------------------- #
#  Shutdown                                                                   #
# --------------------------------------------------------------------------- #


def test_shutdown_drops_queued_jobs_and_cancels_running_ones(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_active_downloads", 1)

    async def scenario():
        svc, runner = make_service()
        runner.block()  # never released: only cancellation ends it
        running = await submit(svc, "https://www.tiktok.com/@a/video/1", user_id=1)
        await asyncio.to_thread(runner.started.wait, 5)
        queued = await submit(svc, "https://www.tiktok.com/@a/video/2", user_id=2)
        await svc.shutdown(grace_sec=5)
        with pytest.raises(Refused, match="shutting_down"):
            svc.admit(-100, 3)
        return svc, running, queued

    svc, running, queued = asyncio.run(scenario())
    assert queued.status.edits[-1].startswith("♻️")
    assert running.status.edits[-1].startswith("♻️")
    assert svc._pending_user == {} and svc.active == 0


def test_status_reports_measured_values(db) -> None:
    async def scenario():
        svc, _ = make_service(db)
        await submit(svc)
        await drain(svc)
        return svc.status(database_ok=True, privacy_mode="on", flood_waits=2)

    status = asyncio.run(scenario())
    assert (status.active, status.queued, status.cache_misses, status.cache_entries) == (0, 0, 1, 1)
    assert status.flood_waits == 2 and status.database_ok is True
    text = texts.bot_status(status, __import__("datetime").datetime.now(settings.tz))
    assert "Кэш: 0 попаданий · 1 промахов" in text and texts.visible_length(text) < 4096
