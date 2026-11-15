"""Telegram handlers, driven with small fakes instead of real PTB objects."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import Conflict, NetworkError, TelegramError

from sbahelper import handlers, service, texts
from sbahelper.cache import FileCache
from sbahelper.config import settings
from sbahelper.db import Database
from sbahelper.prefs import Preferences
from sbahelper.runtime import RUNTIME_KEY, Runtime
from sbahelper.service import DownloadService
from sbahelper.stats import DownloadEvent, StatsStore
from tests.fakes import FakeMessage, FakeRunner, drain

VIDEO_URL = "https://www.tiktok.com/@cat/video/7234567890123"
USER = SimpleNamespace(id=42, full_name="Максим", username="maksim")
OTHER = SimpleNamespace(id=43, full_name="Аня", username=None)


@pytest.fixture(autouse=True)
def fast(monkeypatch) -> None:
    monkeypatch.setattr(service, "MIN_EDIT_INTERVAL_SEC", 0)
    monkeypatch.setattr(service, "REJECTION_TTL_SEC", 0)


class FakeBot:
    def __init__(self, admins: set[int] = frozenset()) -> None:
        self.admins = admins

    async def get_chat_member(self, chat_id: int, user_id: int):
        status = "administrator" if user_id in self.admins else "member"
        return SimpleNamespace(status=status)


@pytest.fixture
def rt(tmp_path: Path) -> Runtime:
    db = Database(tmp_path / "bot.db")
    stats, prefs = StatsStore(db), Preferences(db)
    cache = FileCache(db, bot_id=1, policy=1, ttl_days=30)
    svc = DownloadService(settings, cache=cache, stats=stats, prefs=prefs, runner=FakeRunner())
    return Runtime(service=svc, db=db, stats=stats, cache=cache, prefs=prefs)


def update_for(message: FakeMessage, user=USER, query=None, change=None) -> SimpleNamespace:
    return SimpleNamespace(
        effective_message=message,
        effective_chat=message.chat,
        effective_user=user,
        callback_query=query,
        my_chat_member=change,
        update_id=1,
    )


def context_for(rt: Runtime | None, args: list[str] | None = None, bot=None) -> SimpleNamespace:
    return SimpleNamespace(
        bot_data={RUNTIME_KEY: rt} if rt else {}, args=args or [], bot=bot or FakeBot()
    )


def run(handler, update, context, rt: Runtime | None = None) -> None:
    async def scenario() -> None:
        await handler(update, context)
        if rt is not None:
            await drain(rt.service)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
#  Links                                                                      #
# --------------------------------------------------------------------------- #


def test_link_in_a_group_is_downloaded_and_recorded(rt) -> None:
    message = FakeMessage(f"глянь {VIDEO_URL}")
    run(handlers.on_message, update_for(message), context_for(rt), rt)

    assert message.media[0][1] == b"video"
    assert message.status.deleted
    now = datetime.now(UTC)
    week = rt.stats.period_stats(
        chat_id=-100, start=now - timedelta(days=1), end=now + timedelta(days=1)
    )
    assert week.total_downloads == 1 and week.top_users[0].username == "maksim"


@pytest.mark.parametrize(
    "text",
    ["просто текст", "https://www.tiktok.com/@cat", "https://example.com/video/1"],
)
def test_messages_without_a_video_link_are_ignored(rt, text: str) -> None:
    message = FakeMessage(text)
    run(handlers.on_message, update_for(message), context_for(rt), rt)
    assert message.replies == []


def test_chats_outside_the_allowlist_are_ignored(rt, monkeypatch) -> None:
    monkeypatch.setattr(settings, "access_policy", "allowlist")
    monkeypatch.setattr(settings, "allowed_chat_ids", frozenset({-200}))
    message = FakeMessage(VIDEO_URL)
    run(handlers.on_message, update_for(message), context_for(rt), rt)
    run(handlers.cmd_download, update_for(message), context_for(rt, [VIDEO_URL]), rt)
    assert message.replies == []


def test_auto_download_off_ignores_messages_but_download_command_works(rt) -> None:
    rt.prefs.update_chat(-100, auto_download=False)
    passive = FakeMessage(VIDEO_URL)
    run(handlers.on_message, update_for(passive), context_for(rt), rt)
    assert passive.replies == []

    command = FakeMessage(f"/download {VIDEO_URL}")
    run(handlers.cmd_download, update_for(command), context_for(rt, [VIDEO_URL]), rt)
    assert command.media[0][1] == b"video"


def test_download_as_a_reply_uses_the_replied_message(rt) -> None:
    original = FakeMessage(f"вот {VIDEO_URL}")
    command = FakeMessage("/download")
    command.reply_to_message = original
    run(handlers.cmd_download, update_for(command), context_for(rt), rt)
    assert original.media and not command.media  # the video answers the link


@pytest.mark.parametrize(
    ("args", "expected"), [([], texts.DOWNLOAD_USAGE), (["x"], texts.NO_LINKS)]
)
def test_download_without_a_link_explains_itself(rt, args, expected) -> None:
    message = FakeMessage("/download " + " ".join(args))
    run(handlers.cmd_download, update_for(message), context_for(rt, args), rt)
    assert message.replies[0].text == expected


def test_extra_links_are_skipped_with_one_note(rt, monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_links_per_message", 2)
    links = " ".join(f"https://www.tiktok.com/@a/video/72345678901{i:02d}" for i in range(4))
    message = FakeMessage(links)
    run(handlers.on_message, update_for(message), context_for(rt), rt)
    assert message.replies[0].text == texts.too_many_links(2)
    assert len(message.media) == 2


def test_duplicate_links_in_one_message_count_once(rt) -> None:
    message = FakeMessage(f"{VIDEO_URL} {VIDEO_URL}?is_from_webapp=1")
    run(handlers.on_message, update_for(message), context_for(rt), rt)
    assert len(message.media) == 1


def test_refusal_is_explained(rt, monkeypatch) -> None:
    monkeypatch.setattr(settings, "user_links_per_minute", 1)
    first, second = FakeMessage(VIDEO_URL), FakeMessage(VIDEO_URL)
    run(handlers.on_message, update_for(first), context_for(rt), rt)
    run(handlers.on_message, update_for(second), context_for(rt), rt)
    assert second.replies[0].text == texts.refusal("rate_limited")


# --------------------------------------------------------------------------- #
#  Commands                                                                   #
# --------------------------------------------------------------------------- #


def test_start_help_privacy(rt) -> None:
    message = FakeMessage("/start")
    run(handlers.cmd_start, update_for(message), context_for(rt))
    run(handlers.cmd_help, update_for(message), context_for(rt))
    run(handlers.cmd_privacy, update_for(message), context_for(rt))
    start, help_text, privacy = (reply.text for reply in message.replies)
    assert start == texts.start(5, 45_000_000)
    assert "/download" in help_text and "режим приватности" in help_text
    assert "400 дней" in privacy


def test_stats_without_a_database() -> None:
    message = FakeMessage("/stats")
    svc = DownloadService(settings)
    run(handlers.cmd_stats, update_for(message), context_for(Runtime(service=svc)))
    assert message.replies[0].text == texts.STATS_OFF


@pytest.mark.parametrize(
    ("args", "title"), [([], "Эта неделя"), (["month"], "Этот месяц"), (["день"], "Сегодня")]
)
def test_stats_periods(rt, args, title) -> None:
    message = FakeMessage("/stats")
    run(handlers.cmd_stats, update_for(message), context_for(rt, args))
    reply = message.replies[0]
    assert reply.text.startswith(f"📊 <b>{title}")
    assert isinstance(reply.markups[0], InlineKeyboardMarkup)


def test_settings_in_private_and_in_groups(rt) -> None:
    private = FakeMessage("/settings", chat_id=42)
    run(handlers.cmd_settings, update_for(private), context_for(rt))
    assert private.replies[0].text.startswith("⚙️ <b>Твои настройки")
    group = FakeMessage("/settings")
    run(handlers.cmd_settings, update_for(group), context_for(rt))
    assert group.replies[0].text.startswith("⚙️ <b>Настройки чата")


def test_delete_my_data_asks_first(rt) -> None:
    rt.stats.record(DownloadEvent(-100, 42, "Я", None, "tiktok"))
    message = FakeMessage("/delete_my_data", chat_id=42)
    run(handlers.cmd_delete_my_data, update_for(message), context_for(rt))
    assert "(1 запись)" in message.replies[0].text
    assert rt.stats.user_row_count(42) == 1  # nothing deleted yet


# --------------------------------------------------------------------------- #
#  Callback queries                                                           #
# --------------------------------------------------------------------------- #


class FakeQuery:
    def __init__(self, data: str, user=USER, message: FakeMessage | None = None) -> None:
        self.data = data
        self.from_user = user
        self.message = message or FakeMessage("old")
        self.answers: list[tuple[str | None, bool]] = []
        self.edited: list[tuple[str, object]] = []

    async def answer(self, text=None, show_alert=False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None) -> None:
        self.edited.append((text, reply_markup))


def press(rt, data: str, user=USER, *, chat_id: int = -100, admins=frozenset()) -> FakeQuery:
    query = FakeQuery(data, user, FakeMessage("old", chat_id=chat_id))
    update = update_for(query.message, user, query=query)
    run(handlers.on_callback, update, context_for(rt, bot=FakeBot(set(admins))))
    assert len(query.answers) == 1  # every callback is answered exactly once
    return query


@pytest.mark.parametrize(
    "data", ["", "st:year", "ps:stats:2:42", "gs:auto:1;drop", "x" * 64, "dd:yes:abc"]
)
def test_malformed_callbacks_are_answered_as_stale(rt, data) -> None:
    query = press(rt, data)
    assert query.answers == [(texts.STALE_BUTTON, False)] and query.edited == []


def test_stats_period_button(rt) -> None:
    query = press(rt, "st:day")
    assert query.edited[0][0].startswith("📊 <b>Сегодня")


def test_personal_toggle_only_for_its_owner(rt) -> None:
    refused = press(rt, "ps:stats:0:42", OTHER, chat_id=43)
    assert refused.answers[0][0] == texts.NOT_YOURS and not rt.prefs.user(42).stats_opt_out

    accepted = press(rt, "ps:stats:0:42", USER, chat_id=42)
    assert accepted.answers[0][0] == texts.SAVED and rt.prefs.user(42).stats_opt_out
    press(rt, "ps:stats:1:42", USER, chat_id=42)
    assert not rt.prefs.user(42).stats_opt_out


def test_group_settings_need_a_group_admin_at_press_time(rt) -> None:
    refused = press(rt, "gs:auto:0", USER)
    assert refused.answers[0] == (texts.NOT_GROUP_ADMIN, True)
    assert rt.prefs.chat(-100).auto_download

    accepted = press(rt, "gs:auto:0", USER, admins={42})
    assert accepted.answers[0][0] == texts.SAVED and not rt.prefs.chat(-100).auto_download


def test_bot_admins_are_not_group_admins(rt, monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", frozenset({42}))
    assert press(rt, "gs:auto:0", USER).answers[0][0] == texts.NOT_GROUP_ADMIN


def test_anonymous_admins_are_asked_to_reveal_themselves(rt) -> None:
    anonymous = SimpleNamespace(id=handlers.ANONYMOUS_ADMIN_ID, full_name="Group", username=None)
    assert press(rt, "gs:auto:0", anonymous).answers[0] == (texts.ANONYMOUS_ADMIN, True)


def test_summary_topic_can_be_reset(rt) -> None:
    rt.prefs.update_chat(-100, summary_thread_id=7)
    press(rt, "gs:topic:main", USER, admins={42})
    assert rt.prefs.chat(-100).summary_thread_id is None


def test_callbacks_in_other_chats_are_refused(rt, monkeypatch) -> None:
    monkeypatch.setattr(settings, "access_policy", "allowlist")
    assert press(rt, "st:week").answers[0][0] == texts.NOT_AVAILABLE


def test_data_deletion_confirmed_by_its_owner_only(rt) -> None:
    rt.stats.record(DownloadEvent(-100, 42, "Я", None, "tiktok"))
    rt.stats.record(DownloadEvent(-100, 43, "Аня", None, "tiktok"))

    assert press(rt, "dd:yes:42", OTHER).answers[0] == (texts.NOT_YOURS, True)
    assert rt.stats.user_row_count(42) == 1
    press(rt, "dd:no:42", USER)
    assert rt.stats.user_row_count(42) == 1

    query = press(rt, "dd:yes:42", USER)
    assert query.edited[0][0] == texts.delete_done(1)
    assert rt.stats.user_row_count(42) == 0 and rt.stats.user_row_count(43) == 1


def test_callback_data_fits_telegram_limit() -> None:
    biggest_id = 10**19
    for markup in (
        handlers.stats_keyboard("month"),
        handlers.personal_keyboard(biggest_id, True),
        handlers.group_keyboard(True, 5, True),
        handlers.delete_keyboard(biggest_id),
    ):
        for row in markup.inline_keyboard:
            for button in row:
                data = button.callback_data
                assert len(data.encode()) <= 64 and handlers._CALLBACK.fullmatch(data)


# --------------------------------------------------------------------------- #
#  Admin tools                                                                #
# --------------------------------------------------------------------------- #


def cookie_document(data: bytes, size: int | None = -1, fail: bool = False) -> SimpleNamespace:
    async def download_as_bytearray() -> bytearray:
        if fail:
            raise TelegramError("file is too big")
        return bytearray(data)

    async def get_file() -> SimpleNamespace:
        return SimpleNamespace(download_as_bytearray=download_as_bytearray)

    return SimpleNamespace(file_size=len(data) if size == -1 else size, get_file=get_file)


GOOD_COOKIES = b".tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc\n"


def test_uploaded_cookies_are_saved_and_the_file_message_deleted() -> None:
    message = FakeMessage(document=cookie_document(GOOD_COOKIES), chat_id=42)
    run(handlers.on_cookie_file, update_for(message), context_for(None))
    assert (settings.cookies_dir / "tiktok.txt").exists()
    assert message.deleted
    assert message.chat_messages == [texts.cookies_saved({"tiktok": 1})]


def test_failed_deletion_is_admitted() -> None:
    message = FakeMessage(document=cookie_document(GOOD_COOKIES), chat_id=42)
    message.delete_error = TelegramError("Message can't be deleted")
    run(handlers.on_cookie_file, update_for(message), context_for(None))
    assert message.chat_messages == [texts.cookies_saved({"tiktok": 1}, deleted=False)]


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (cookie_document(b"hello world"), "line 1"),
        (cookie_document(b"\xff\xfe\x00"), "UTF-8"),
        (cookie_document(b"x", size=5 * 1024 * 1024), "larger than 1 MB"),
        (cookie_document(b"x" * (2 * 1024 * 1024), size=10), "larger than 1 MB"),  # size lied
        (cookie_document(GOOD_COOKIES, size=None), "size is unknown"),
        (cookie_document(GOOD_COOKIES, fail=True), "did not hand over"),
    ],
)
def test_bad_uploads_are_explained(document, reason: str) -> None:
    message = FakeMessage(document=document, chat_id=42)
    run(handlers.on_cookie_file, update_for(message), context_for(None))
    assert reason in message.replies[0].text
    assert not message.deleted
    assert not settings.cookies_dir.exists()


def test_cookie_status_command() -> None:
    message = FakeMessage("/cookies", chat_id=42)
    run(handlers.cmd_cookies, update_for(message), context_for(None))
    assert message.replies[0].text.startswith("🍪 <b>Cookies</b>")


def test_status_command(rt) -> None:
    message = FakeMessage("/status", chat_id=42)
    run(handlers.cmd_status, update_for(message), context_for(rt))
    assert message.replies[0].text.startswith("🩺 <b>Состояние бота</b>")
    assert "База данных: ✅" in message.replies[0].text


# --------------------------------------------------------------------------- #
#  Membership, migration, errors                                              #
# --------------------------------------------------------------------------- #


def test_removal_marks_the_chat_inactive_and_return_reactivates(rt) -> None:
    def change(status: str) -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=-100), new_chat_member=SimpleNamespace(status=status)
        )

    message = FakeMessage()
    run(handlers.on_my_chat_member, update_for(message, change=change("kicked")), context_for(rt))
    assert rt.prefs.chat(-100).active is False
    run(handlers.on_my_chat_member, update_for(message, change=change("member")), context_for(rt))
    assert rt.prefs.chat(-100).active is True


def test_group_migration_moves_statistics(rt) -> None:
    rt.stats.record(DownloadEvent(-100, 42, "Я", None, "tiktok"))
    message = FakeMessage(chat_id=-100)
    message.migrate_to_chat_id = -1001
    run(handlers.on_migrate, update_for(message), context_for(rt))
    assert rt.stats.active_chat_ids(
        datetime.now(UTC) - timedelta(days=1), datetime.now(UTC) + timedelta(days=1)
    ) == [-1001]


def test_network_errors_are_only_warnings(caplog) -> None:
    asyncio.run(handlers.on_error(None, SimpleNamespace(error=NetworkError("Bad Gateway"))))
    assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_second_polling_instance_alerts(caplog) -> None:
    asyncio.run(
        handlers.on_error(None, SimpleNamespace(error=Conflict("terminated by other getUpdates")))
    )
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
