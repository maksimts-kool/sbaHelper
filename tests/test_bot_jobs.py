"""
Обвязка бота вокруг статистики — `downloader/bot.py`.

Нужен настоящий python-telegram-bot (заглушек в conftest для `telegram.ext`
не хватает), поэтому без установленного пакета модуль пропускается.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("telegram.ext", reason="python-telegram-bot is not installed")

from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder

from downloader.bot import (
    STATS_STORE_KEY,
    STATS_TZ,
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
