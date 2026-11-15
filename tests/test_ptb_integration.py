"""The download service through real python-telegram-bot objects.

Only the HTTP layer is fake (a BaseRequest that records calls), so every
Message shortcut and Bot method runs with its real signature, the real
Pacer and real request serialisation. A wrong keyword argument fails here.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from telegram import Message
from telegram.ext import ExtBot
from telegram.request import BaseRequest, RequestData

from sbahelper import jobs, service, texts
from sbahelper.config import settings
from sbahelper.links import parse_link
from sbahelper.pacing import Pacer
from sbahelper.service import DownloadService, Request, StatusMessage
from tests.fakes import FakeRunner, drain

TOKEN = "123456:" + "A" * 35
CHAT = {"id": -100, "type": "supergroup", "title": "g", "is_forum": True}
URL = "https://www.tiktok.com/@cat/video/7234567890123"


class RecordingRequest(BaseRequest):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 100

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 5.0

    def _message(self, **extra) -> dict:
        self._next_id += 1
        now = int(datetime.now(UTC).timestamp())
        return {"message_id": self._next_id, "date": now, "chat": CHAT, **extra}

    async def do_request(self, url, method, request_data: RequestData | None = None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            result: object = {"id": 123456, "is_bot": True, "first_name": "T", "username": "t_bot"}
        elif endpoint in ("sendMessage", "editMessageText"):
            result = self._message(text=params.get("text", ""))
        elif endpoint == "sendVideo":
            result = self._message(
                video={
                    "file_id": "REAL-FILE-ID",
                    "file_unique_id": "u",
                    "width": 576,
                    "height": 1024,
                    "duration": 14,
                }
            )
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


async def real_bot() -> tuple[ExtBot, RecordingRequest]:
    request = RecordingRequest()
    bot = ExtBot(TOKEN, request=request, get_updates_request=request, rate_limiter=Pacer())
    await bot.initialize()
    return bot, request


def real_message(bot: ExtBot, text: str, *, thread: int | None = 7) -> Message:
    data = {
        "message_id": 5,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": CHAT,
        "text": text,
        "message_thread_id": thread,
        "is_topic_message": thread is not None,
    }
    return Message.de_json(data, bot)


def test_full_delivery_uses_valid_telegram_calls(monkeypatch) -> None:
    monkeypatch.setattr(service, "MIN_EDIT_INTERVAL_SEC", 0)

    async def scenario() -> list[tuple[str, dict]]:
        bot, request = await real_bot()
        svc = DownloadService(settings, runner=FakeRunner())
        message = real_message(bot, URL)
        status = StatusMessage(await message.reply_text(texts.CHECKING))
        ticket = svc.admit(-100, 42)
        await svc.submit(Request(parse_link(URL), message, status, ticket, -100, 42))
        await drain(svc)
        await bot.shutdown()
        return request.calls

    calls = asyncio.run(scenario())
    endpoints = [endpoint for endpoint, _ in calls]
    assert "editMessageText" in endpoints  # progress / sending status
    assert "sendVideo" in endpoints and "deleteMessage" in endpoints
    video = next(params for endpoint, params in calls if endpoint == "sendVideo")
    assert video["message_thread_id"] == 7  # stays in the forum topic
    assert video["supports_streaming"] is True and video["width"] == 576
    assert video["reply_parameters"]["message_id"] == 5


def test_progress_edit_and_chat_action_are_low_priority_bot_calls() -> None:
    async def scenario() -> list[tuple[str, dict]]:
        bot, request = await real_bot()
        message = real_message(bot, URL)
        status = StatusMessage(await message.reply_text(texts.CHECKING))
        await status._edit("progress", low=True)
        await status.note(texts.QUEUED)
        svc = DownloadService(settings)
        task = asyncio.create_task(svc._keep_uploading_action(message))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await status.show("final")
        await bot.shutdown()
        return request.calls

    calls = asyncio.run(scenario())
    edits = [params["text"] for endpoint, params in calls if endpoint == "editMessageText"]
    assert edits == ["progress", texts.QUEUED, "final"]
    action = next(params for endpoint, params in calls if endpoint == "sendChatAction")
    assert action["action"] == "upload_video" and action["message_thread_id"] == 7


def test_cached_video_is_sent_by_file_id(monkeypatch) -> None:
    from sbahelper.cache import CachedVideo
    from tests.fakes import INFO

    async def scenario() -> list[tuple[str, dict]]:
        bot, request = await real_bot()
        svc = DownloadService(settings)
        message = real_message(bot, URL, thread=None)
        for kind in ("video", "animation", "document"):
            ok = await svc._send_cached(
                Request(parse_link(URL), message, None, None, -100, 42),  # type: ignore[arg-type]
                "FILE",
                kind,
                INFO,
            )
            assert ok
        hit = CachedVideo("tiktok:7234567890123", "FILE", "video", INFO, 5)
        status = StatusMessage(await message.reply_text(texts.CHECKING))
        await svc._serve_cached(
            Request(parse_link(URL), message, status, svc.admit(-100, 42), -100, 42), hit
        )
        await bot.shutdown()
        return request.calls

    endpoints = [endpoint for endpoint, _ in asyncio.run(scenario())]
    assert {"sendVideo", "sendAnimation", "sendDocument"} <= set(endpoints)


def test_weekly_summary_send_is_valid(monkeypatch) -> None:
    from types import SimpleNamespace

    from sbahelper.runtime import Runtime

    async def scenario() -> list[tuple[str, dict]]:
        bot, request = await real_bot()
        context = SimpleNamespace(bot=bot)
        rt = Runtime(service=DownloadService(settings))
        assert await jobs._send_summary(context, rt, -100, "summary")
        await bot.shutdown()
        return request.calls

    calls = asyncio.run(scenario())
    assert calls[-1][0] == "sendMessage" and calls[-1][1]["text"] == "summary"
