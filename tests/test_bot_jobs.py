"""
Обвязка бота вокруг статистики — `downloader/bot.py`.

Нужен настоящий python-telegram-bot (заглушек в conftest для `telegram.ext`
не хватает), поэтому без установленного пакета модуль пропускается.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("telegram.ext", reason="python-telegram-bot is not installed")

from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder

from downloader.bot import (
    MAX_DOWNLOAD_ATTEMPTS,
    PENDING_RETRIES_KEY,
    RETRY_MAX_OVERDUE_SEC,
    STATS_STORE_KEY,
    STATS_TZ,
    DownloadRequest,
    PendingRetry,
    _fail,
    _resume_pending_retries,
    _retry_download_job,
    _send_weekly_stats,
    schedule_weekly_stats,
)
from downloader.stats import DownloadEvent, StatsStore

UTC = timezone.utc
PERIOD = (datetime(2026, 7, 20, tzinfo=UTC), datetime(2026, 7, 27, tzinfo=UTC))


class FakeBot:
    def __init__(self, *, failing_chat_ids: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self._failing = failing_chat_ids or set()

    async def send_message(self, *, chat_id: int, text: str, parse_mode: str | None = None):
        if chat_id in self._failing:
            raise TelegramError("bot was kicked from the group chat")
        self.sent.append((chat_id, text))


@pytest.fixture
def store(tmp_path: Path) -> StatsStore:
    return StatsStore(str(tmp_path / "stats.db"))


def record(store: StatsStore, chat_id: int, name: str, when: datetime, **kwargs) -> None:
    store.record(
        DownloadEvent(
            chat_id=chat_id,
            user_id=abs(hash(name)) % 10_000,
            user_name=name,
            username=kwargs.pop("username", None),
            platform=kwargs.pop("platform", "tiktok"),
            **kwargs,
        ),
        at=when,
    )


def run_weekly(store: StatsStore, bot: FakeBot, *, allowed: set[int] | None = None) -> None:
    """Гоняет недельную рассылку по фиксированному периоду.

    `ALLOWED_CHAT_IDS` подменяется всегда: иначе тест зависел бы от `.env`
    разработчика, который `load_dotenv()` подхватывает при импорте.
    """
    context = SimpleNamespace(bot_data={STATS_STORE_KEY: store}, bot=bot)
    with patch("downloader.bot.week_period", return_value=PERIOD):
        with patch("downloader.bot.ALLOWED_CHAT_IDS", allowed if allowed else set()):
            asyncio.run(_send_weekly_stats(context))


# --------------------------------------------------------------------------- #
#  Расписание                                                                  #
# --------------------------------------------------------------------------- #


def build_application():
    return ApplicationBuilder().token("123456:TEST").build()


def test_weekly_job_fires_on_the_configured_day_and_time(store: StatsStore) -> None:
    app = build_application()
    app.bot_data[STATS_STORE_KEY] = store

    schedule_weekly_stats(app)

    jobs = app.job_queue.jobs()
    assert [job.name for job in jobs] == ["weekly-stats"]

    # Считаем от среды, чтобы следующий запуск точно был по расписанию.
    wednesday = datetime(2026, 7, 29, 12, 0, tzinfo=STATS_TZ)
    next_run = jobs[0].job.trigger.get_next_fire_time(None, wednesday)

    assert next_run.weekday() == 6  # воскресенье
    assert (next_run.hour, next_run.minute) == (20, 0)
    assert next_run.tzinfo == STATS_TZ


def test_nothing_is_scheduled_without_a_store() -> None:
    app = build_application()
    app.bot_data[STATS_STORE_KEY] = None

    schedule_weekly_stats(app)

    assert app.job_queue.jobs() == ()


# --------------------------------------------------------------------------- #
#  Рассылка сводки                                                             #
# --------------------------------------------------------------------------- #


def test_each_chat_gets_only_its_own_numbers(store: StatsStore) -> None:
    record(store, -100, "Максим", datetime(2026, 7, 21, 12, tzinfo=UTC))
    record(store, -100, "Максим", datetime(2026, 7, 22, 12, tzinfo=UTC))
    record(store, -200, "Аня", datetime(2026, 7, 22, 12, tzinfo=UTC))
    bot = FakeBot()

    run_weekly(store, bot)

    sent = dict(bot.sent)
    assert set(sent) == {-100, -200}
    assert "Скачано: <b>2</b> видео" in sent[-100]
    assert "Максим — 2" in sent[-100]
    assert "Скачано: <b>1</b> видео" in sent[-200]
    assert "Максим" not in sent[-200]


def test_chats_without_downloads_stay_silent(store: StatsStore) -> None:
    record(store, -100, "Максим", datetime(2026, 7, 10, tzinfo=UTC))  # прошлая неделя
    bot = FakeBot()

    run_weekly(store, bot)

    assert bot.sent == []


def test_disallowed_chats_are_skipped(store: StatsStore) -> None:
    record(store, -100, "Максим", datetime(2026, 7, 21, 12, tzinfo=UTC))
    record(store, -200, "Аня", datetime(2026, 7, 21, 12, tzinfo=UTC))
    bot = FakeBot()

    run_weekly(store, bot, allowed={-200})

    assert [chat_id for chat_id, _ in bot.sent] == [-200]


def test_one_failing_chat_does_not_stop_the_rest(store: StatsStore) -> None:
    record(store, -100, "Максим", datetime(2026, 7, 21, 12, tzinfo=UTC))
    record(store, -200, "Аня", datetime(2026, 7, 21, 12, tzinfo=UTC))
    bot = FakeBot(failing_chat_ids={-200})

    run_weekly(store, bot)

    assert [chat_id for chat_id, _ in bot.sent] == [-100]


def test_old_rows_are_pruned_after_the_weekly_run(store: StatsStore) -> None:
    record(store, -100, "Максим", datetime(2026, 7, 21, 12, tzinfo=UTC))
    record(store, -100, "Максим", datetime(2020, 1, 1, tzinfo=UTC))

    with patch("downloader.bot.STATS_RETENTION_DAYS", 30):
        run_weekly(store, FakeBot())

    ancient = store.weekly_stats(
        chat_id=-100,
        start=datetime(2019, 1, 1, tzinfo=UTC),
        end=datetime(2021, 1, 1, tzinfo=UTC),
    )
    assert ancient.total_downloads == 0


def test_retention_zero_keeps_everything(store: StatsStore) -> None:
    record(store, -100, "Максим", datetime(2026, 7, 21, 12, tzinfo=UTC))
    record(store, -100, "Максим", datetime(2020, 1, 1, tzinfo=UTC))

    with patch("downloader.bot.STATS_RETENTION_DAYS", 0):
        run_weekly(store, FakeBot())

    ancient = store.weekly_stats(
        chat_id=-100,
        start=datetime(2019, 1, 1, tzinfo=UTC),
        end=datetime(2021, 1, 1, tzinfo=UTC),
    )
    assert ancient.total_downloads == 1


def test_missing_store_is_a_no_op() -> None:
    bot = FakeBot()
    context = SimpleNamespace(bot_data={STATS_STORE_KEY: None}, bot=bot)

    asyncio.run(_send_weekly_stats(context))

    assert bot.sent == []


def test_store_survives_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "stats.db")
    record(StatsStore(path), -100, "Максим", datetime(2026, 7, 21, 12, tzinfo=UTC))
    bot = FakeBot()

    run_weekly(StatsStore(path), bot)

    assert len(bot.sent) == 1


# --------------------------------------------------------------------------- #
#  Повтор неудачной загрузки                                                   #
# --------------------------------------------------------------------------- #


class FakeJobQueue:
    def __init__(self) -> None:
        self.jobs: list[SimpleNamespace] = []

    def run_once(self, callback, when, *, data=None, name=None, chat_id=None, **kwargs):
        self.jobs.append(
            SimpleNamespace(callback=callback, when=when, data=data, name=name, chat_id=chat_id)
        )


class FakeStatusMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit_text(self, text: str, parse_mode: str | None = None) -> None:
        self.texts.append(text)


def make_request(attempt: int = 1) -> DownloadRequest:
    return DownloadRequest(
        url="https://www.tiktok.com/@user/video/1",
        chat_id=-100,
        chat_type="supergroup",
        chat_label="chat (id=-100)",
        user_label="Максим (id=1)",
        user=None,
        reply_to_message_id=7,
        status_message_id=8,
        attempt=attempt,
    )


def run_fail(request: DownloadRequest, *, delay: int = 120, chat_data: dict | None = None):
    queue = FakeJobQueue()
    status = FakeStatusMessage()
    context = SimpleNamespace(job_queue=queue, chat_data={} if chat_data is None else chat_data)

    with patch("downloader.bot.RETRY_DELAY_SEC", delay):
        asyncio.run(_fail(context, request, status, "ошибка загрузки"))

    return queue, status, context.chat_data


def test_first_failure_schedules_one_retry() -> None:
    queue, status, chat_data = run_fail(make_request())

    assert len(queue.jobs) == 1
    job = queue.jobs[0]
    assert job.when == 120
    assert job.chat_id == -100
    assert job.data.attempt == 2
    assert job.data.url == make_request().url
    assert "Попробую ещё раз" in status.texts[0]

    pending = chat_data[PENDING_RETRIES_KEY]
    assert list(pending) == [7]
    assert pending[7].request.attempt == 2


def test_second_failure_is_forgotten() -> None:
    queue, status, chat_data = run_fail(make_request(attempt=MAX_DOWNLOAD_ATTEMPTS))

    assert queue.jobs == []
    assert chat_data.get(PENDING_RETRIES_KEY, {}) == {}
    assert status.texts[0] == "ошибка загрузки"


def test_zero_delay_disables_retries() -> None:
    queue, status, chat_data = run_fail(make_request(), delay=0)

    assert queue.jobs == []
    assert chat_data.get(PENDING_RETRIES_KEY, {}) == {}
    assert status.texts[0] == "ошибка загрузки"


def test_missing_job_queue_does_not_break_the_error_message() -> None:
    status = FakeStatusMessage()
    context = SimpleNamespace(job_queue=None, chat_data={})

    with patch("downloader.bot.RETRY_DELAY_SEC", 120):
        asyncio.run(_fail(context, make_request(), status, "ошибка загрузки"))

    assert status.texts[0] == "ошибка загрузки"


def test_retry_job_reruns_the_request_and_clears_it_from_chat_data() -> None:
    request = make_request(attempt=2)
    chat_data = {PENDING_RETRIES_KEY: {7: PendingRetry(request=request, run_at=0.0)}}
    context = SimpleNamespace(
        job=SimpleNamespace(data=request),
        chat_data=chat_data,
        bot=FakeBot(),
    )
    seen = []

    async def fake_process(ctx, req, msg):
        seen.append((req, msg.message_id, msg.chat_id))

    with patch("downloader.bot._process_download", fake_process):
        asyncio.run(_retry_download_job(context))

    assert seen == [(request, 8, -100)]
    assert chat_data[PENDING_RETRIES_KEY] == {}


# --------------------------------------------------------------------------- #
#  Повторы переживают перезапуск                                               #
# --------------------------------------------------------------------------- #


def resume(pending: dict) -> tuple[FakeJobQueue, dict]:
    chat_data = {PENDING_RETRIES_KEY: pending}
    app = SimpleNamespace(job_queue=FakeJobQueue(), chat_data={-100: chat_data})

    asyncio.run(_resume_pending_retries(app))

    return app.job_queue, pending


def test_pending_retry_is_rearmed_after_a_restart() -> None:
    request = make_request(attempt=2)
    run_at = time.time() + 90
    queue, pending = resume({7: PendingRetry(request=request, run_at=run_at)})

    assert len(queue.jobs) == 1
    job = queue.jobs[0]
    assert job.data == request
    assert job.chat_id == -100
    assert 80 <= job.when <= 90
    assert list(pending) == [7]


def test_overdue_retry_runs_immediately() -> None:
    request = make_request(attempt=2)
    queue, _ = resume({7: PendingRetry(request=request, run_at=time.time() - 30)})

    assert len(queue.jobs) == 1
    assert queue.jobs[0].when == 0.0


def test_stale_retry_is_dropped() -> None:
    request = make_request(attempt=2)
    run_at = time.time() - RETRY_MAX_OVERDUE_SEC - 60
    queue, pending = resume({7: PendingRetry(request=request, run_at=run_at)})

    assert queue.jobs == []
    assert pending == {}
