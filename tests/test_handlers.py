"""Telegram handlers, driven with small fakes instead of real PTB objects."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import NetworkError, TelegramError

from sbahelper import handlers, texts
from sbahelper.config import settings
from sbahelper.download import DownloadError, DownloadResult, LoginRequired, Rejected, VideoInfo
from sbahelper.handlers import STATS_KEY, StatusMessage
from sbahelper.stats import StatsStore

VIDEO_URL = "https://www.tiktok.com/@cat/video/1"
INFO = VideoInfo("tiktok", "Кот", "cat", 14, view_count=1500, width=576, height=1024)


class FakeMessage:
    def __init__(self, text: str = "", *, chat_id: int = -100, document=None) -> None:
        self.text = text
        self.caption = None
        self.chat_id = chat_id
        self.document = document
        self.chat = SimpleNamespace(id=chat_id, send_message=self._send_to_chat)
        self.replies: list[FakeMessage] = []
        self.videos: list[tuple[bytes, dict]] = []
        self.edits: list[str] = []
        self.chat_messages: list[str] = []
        self.deleted = False
        self.video_error: Exception | None = None

    def build_reply_arguments(self, **kwargs) -> dict:
        return {"allow_sending_without_reply": True}

    async def reply_text(self, text: str, **kwargs) -> FakeMessage:
        reply = FakeMessage(text, chat_id=self.chat_id)
        self.replies.append(reply)
        return reply

    async def reply_video(self, video, **kwargs) -> None:
        if self.video_error:
            raise self.video_error
        self.videos.append((video.read(), kwargs))

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)
        self.text = text

    async def delete(self) -> None:
        self.deleted = True

    async def _send_to_chat(self, text: str, **kwargs) -> None:
        self.chat_messages.append(text)

    @property
    def status(self) -> FakeMessage:
        return self.replies[0]


USER = SimpleNamespace(id=42, full_name="Максим", username="maksim")


def update_for(message: FakeMessage, user=USER) -> SimpleNamespace:
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=message.chat_id),
        effective_user=user,
    )


def context_for(store: StatsStore | None = None) -> SimpleNamespace:
    tasks: list[asyncio.Task] = []
    return SimpleNamespace(
        bot_data={STATS_KEY: store},
        application=SimpleNamespace(
            create_task=lambda coro: tasks.append(asyncio.ensure_future(coro))
        ),
        tasks=tasks,
    )


@pytest.fixture(autouse=True)
def fast_status(monkeypatch) -> None:
    monkeypatch.setattr(handlers, "MIN_EDIT_INTERVAL_SEC", 0)
    monkeypatch.setattr(handlers, "REJECTION_TTL_SEC", 0)


def fake_download(tmp_path: Path, outcome=None):
    """Replaces download_video: reports progress from the worker thread, then returns or raises."""

    def run(url, *, on_info, on_progress):
        if isinstance(outcome, Exception):
            raise outcome
        on_info(INFO)
        for percent in (5, 42, 100):
            on_progress(percent)
        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / "video.mp4").write_bytes(b"video")
        return DownloadResult(workdir / "video.mp4", INFO, 5)

    return run


def handle(message: FakeMessage, context=None, user=USER) -> SimpleNamespace:
    context = context or context_for()

    async def scenario() -> None:
        await handlers.on_message(update_for(message, user), context)
        await asyncio.gather(*context.tasks)

    asyncio.run(scenario())
    return context


# --------------------------------------------------------------------------- #
#  Links                                                                      #
# --------------------------------------------------------------------------- #


def test_video_is_sent_and_recorded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(handlers, "download_video", fake_download(tmp_path))
    store = StatsStore(tmp_path / "stats.db")
    message = FakeMessage(f"глянь {VIDEO_URL}")

    handle(message, context_for(store))

    assert message.status.text != texts.CHECKING
    assert message.status.deleted
    data, kwargs = message.videos[0]
    assert data == b"video"
    assert kwargs["caption"] == texts.caption(INFO)
    assert (kwargs["width"], kwargs["height"], kwargs["duration"]) == (576, 1024, 14)
    assert not (tmp_path / "work").exists()

    now = datetime.now(UTC)
    week = store.weekly_stats(
        chat_id=-100, start=now - timedelta(days=1), end=now + timedelta(days=1)
    )
    assert week.total_downloads == 1 and week.top_users[0].username == "maksim"


def test_progress_is_shown_while_downloading(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(handlers, "download_video", fake_download(tmp_path))
    message = FakeMessage(VIDEO_URL)

    handle(message)

    edits = message.status.edits
    assert edits[-1] == texts.status(INFO, texts.SENDING)
    progress = [edit for edit in edits if "%" in edit]
    assert progress and all(texts.status(INFO, "") in edit for edit in progress)


def test_rejection_is_flashed_then_removed(monkeypatch, tmp_path: Path) -> None:
    rejection = Rejected(texts.HORIZONTAL, "horizontal 1920x1080")
    monkeypatch.setattr(handlers, "download_video", fake_download(tmp_path, rejection))
    message = FakeMessage(VIDEO_URL)

    handle(message)

    assert message.status.edits == [texts.HORIZONTAL]
    assert message.status.deleted
    assert message.videos == []


def test_failure_stays_in_chat(monkeypatch, tmp_path: Path, caplog) -> None:
    failure = DownloadError(texts.failed("Video unavailable"), "Video unavailable")
    monkeypatch.setattr(handlers, "download_video", fake_download(tmp_path, failure))
    message = FakeMessage(VIDEO_URL)

    with caplog.at_level(logging.INFO):
        handle(message)

    assert message.status.edits == [failure.text]
    assert not message.status.deleted
    assert caplog.records[-1].levelno == logging.WARNING


def test_login_wall_alerts_the_admins(monkeypatch, tmp_path: Path, caplog) -> None:
    failure = LoginRequired(texts.LOGIN, "Sign in to confirm you're not a bot")
    monkeypatch.setattr(handlers, "download_video", fake_download(tmp_path, failure))
    message = FakeMessage(VIDEO_URL)

    handle(message)

    assert message.status.edits == [texts.LOGIN]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "Send fresh cookies" in errors[0].getMessage()


def test_crash_shows_a_generic_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(handlers, "download_video", fake_download(tmp_path, KeyError("x")))
    message = FakeMessage(VIDEO_URL)

    handle(message)

    assert message.status.edits == [texts.UNEXPECTED]


def test_send_failure_is_reported_and_the_file_removed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(handlers, "download_video", fake_download(tmp_path))
    message = FakeMessage(VIDEO_URL)
    message.video_error = TelegramError("Request Entity Too Large")

    handle(message)

    assert message.status.edits[-1] == texts.send_failed("Request Entity Too Large")
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize(
    "text",
    ["просто текст", "https://www.tiktok.com/@cat", "https://example.com/video/1"],
)
def test_messages_without_a_video_link_are_ignored(text: str) -> None:
    message = FakeMessage(text)
    handle(message)
    assert message.replies == []


def test_other_chats_are_ignored(monkeypatch) -> None:
    monkeypatch.setattr(settings, "allowed_chat_ids", frozenset({-200}))
    message = FakeMessage(VIDEO_URL)
    handle(message)
    assert message.replies == []


# --------------------------------------------------------------------------- #
#  Status message                                                             #
# --------------------------------------------------------------------------- #


def test_final_text_is_never_overwritten_by_late_progress() -> None:
    async def scenario() -> FakeMessage:
        message = FakeMessage(texts.CHECKING)
        status = StatusMessage(message)
        status._info = INFO
        await status.show("final")
        status._percent = 90
        await status._refresh()
        return message

    assert asyncio.run(scenario()).edits == ["final"]


def test_identical_edits_are_skipped() -> None:
    async def scenario() -> FakeMessage:
        message = FakeMessage(texts.CHECKING)
        status = StatusMessage(message)
        await status.show("same")
        await status.show("same")
        return message

    assert asyncio.run(scenario()).edits == ["same"]


@pytest.mark.parametrize(
    ("error", "calls"),
    [("Message to delete not found", 1), ("Bad Gateway", 3)],
)
def test_delete_retries_only_real_failures(monkeypatch, error: str, calls: int) -> None:
    attempts = []

    async def failing_delete() -> None:
        attempts.append(1)
        raise TelegramError(error)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _: real_sleep(0))

    async def scenario() -> None:
        message = FakeMessage()
        message.delete = failing_delete
        await StatusMessage(message).delete()

    asyncio.run(scenario())
    assert len(attempts) == calls


# --------------------------------------------------------------------------- #
#  Commands and admin tools                                                   #
# --------------------------------------------------------------------------- #


def test_start_and_disabled_stats() -> None:
    message = FakeMessage("/start")
    asyncio.run(handlers.cmd_start(update_for(message), context_for()))
    asyncio.run(handlers.cmd_stats(update_for(message), context_for(None)))
    assert message.replies[0].text == texts.start(5, 50)
    assert message.replies[1].text == texts.STATS_OFF


def test_stats_command_shows_this_week(tmp_path: Path) -> None:
    message = FakeMessage("/stats")
    asyncio.run(handlers.cmd_stats(update_for(message), context_for(StatsStore(tmp_path / "s.db"))))
    assert message.replies[0].text.startswith(f"📊 <b>{texts.THIS_WEEK}")


def cookie_document(data: bytes, size: int | None = None) -> SimpleNamespace:
    async def download_as_bytearray() -> bytearray:
        return bytearray(data)

    async def get_file() -> SimpleNamespace:
        return SimpleNamespace(download_as_bytearray=download_as_bytearray)

    return SimpleNamespace(file_size=size or len(data), get_file=get_file)


def test_uploaded_cookies_are_saved_and_the_file_message_deleted() -> None:
    data = b".tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc\n"
    message = FakeMessage(document=cookie_document(data), chat_id=42)

    asyncio.run(handlers.on_cookie_file(update_for(message), context_for()))

    assert (settings.cookies_dir / "tiktok.txt").exists()
    assert message.deleted
    assert message.chat_messages == [texts.cookies_saved({"tiktok": 1})]


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (cookie_document(b"hello world"), "line 1"),
        (cookie_document(b"\xff\xfe\x00"), "UTF-8"),
        (cookie_document(b"x", size=5 * 1024 * 1024), "larger than 1 MB"),
    ],
)
def test_bad_uploads_are_explained(document, reason: str) -> None:
    message = FakeMessage(document=document, chat_id=42)

    asyncio.run(handlers.on_cookie_file(update_for(message), context_for()))

    assert reason in message.replies[0].text
    assert not message.deleted
    assert not settings.cookies_dir.exists()


def test_cookie_status_command() -> None:
    message = FakeMessage("/cookies", chat_id=42)
    asyncio.run(handlers.cmd_cookies(update_for(message), context_for()))
    assert message.replies[0].text.startswith("🍪 <b>Cookies</b>")


def test_network_errors_are_only_warnings(caplog) -> None:
    context = SimpleNamespace(error=NetworkError("Bad Gateway"))
    asyncio.run(handlers.on_error(None, context))
    assert [r.levelname for r in caplog.records] == ["WARNING"]
