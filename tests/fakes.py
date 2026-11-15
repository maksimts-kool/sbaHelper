"""Small stand-ins for python-telegram-bot objects and the downloader."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

from sbahelper import download
from sbahelper.download import DownloadResult, VideoInfo

INFO = VideoInfo(
    "tiktok", "Кот", "cat", 14, view_count=1500, width=576, height=1024, video_id="7234567890123"
)


class FakeMessage:
    _ids = iter(range(1000, 10**6))

    def __init__(
        self,
        text: str = "",
        *,
        chat_id: int = -100,
        document=None,
        caption: str | None = None,
        thread_id: int | None = None,
        chat_type: str | None = None,
    ) -> None:
        self.text = text
        self.caption = caption
        self.chat_id = chat_id
        self.message_id = next(self._ids)
        self.document = document
        self.is_topic_message = thread_id is not None
        self.message_thread_id = thread_id
        self.reply_to_message: FakeMessage | None = None
        self.migrate_to_chat_id = None
        kind = chat_type or ("private" if chat_id > 0 else "supergroup")
        self.chat = SimpleNamespace(id=chat_id, type=kind, send_message=self._send_to_chat)
        self.replies: list[FakeMessage] = []
        self.media: list[tuple[str, object, dict]] = []
        self.edits: list[str] = []
        self.chat_messages: list[str] = []
        self.actions: list[str] = []
        self.deleted = False
        self.video_errors: list[Exception] = []
        self.delete_error: Exception | None = None
        self.file_id = "FILE-1"
        self.markups: list[object] = []

    # Entity helpers: no entities, so links come from the text scan.
    def parse_entities(self, types=None) -> dict:
        return {}

    def parse_caption_entities(self, types=None) -> dict:
        return {}

    def build_reply_arguments(self, **kwargs) -> dict:
        return {"allow_sending_without_reply": True, "message_id": self.message_id}

    async def reply_text(self, text: str, **kwargs) -> FakeMessage:
        reply = FakeMessage(text, chat_id=self.chat_id)
        if "reply_markup" in kwargs:
            reply.markups.append(kwargs["reply_markup"])
        self.replies.append(reply)
        return reply

    async def _send_media(self, kind: str, media, kwargs: dict):
        if self.video_errors:
            raise self.video_errors.pop(0)
        payload = media.read() if hasattr(media, "read") else media
        self.media.append((kind, payload, kwargs))
        return SimpleNamespace(
            video=SimpleNamespace(file_id=self.file_id) if kind == "video" else None,
            animation=None,
            document=None,
        )

    async def reply_video(self, video, **kwargs):
        return await self._send_media("video", video, kwargs)

    async def reply_animation(self, animation, **kwargs):
        return await self._send_media("animation", animation, kwargs)

    async def reply_document(self, document, **kwargs):
        return await self._send_media("document", document, kwargs)

    async def reply_chat_action(self, action, **kwargs) -> None:
        self.actions.append(action)

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)
        self.text = text

    async def delete(self) -> None:
        if self.delete_error:
            raise self.delete_error
        self.deleted = True

    async def _send_to_chat(self, text: str, **kwargs) -> None:
        self.chat_messages.append(text)

    @property
    def status(self) -> FakeMessage:
        return self.replies[0]


class FakeRunner:
    """Replaces `download_video`: reports progress, writes a file, optionally blocks."""

    def __init__(self, *, info: VideoInfo = INFO, error: BaseException | None = None) -> None:
        self.info = info
        self.error = error
        self.calls: list[str] = []
        self.gate: threading.Event | None = None
        self.started = threading.Event()
        self.workdirs: list[Path] = []

    def block(self) -> threading.Event:
        self.gate = threading.Event()
        return self.gate

    def __call__(self, url, *, on_info=None, on_progress=None, cancel=None) -> DownloadResult:
        self.calls.append(url)
        self.started.set()
        if self.gate is not None:
            while not self.gate.wait(0.01):
                if cancel is not None:
                    cancel.check()
        if self.error is not None:
            raise self.error
        if on_info:
            on_info(self.info)
        for percent in (5, 42, 100):
            if on_progress:
                on_progress(percent)
        workdir = download.make_workdir()
        self.workdirs.append(workdir)
        path = workdir / "video.mp4"
        path.write_bytes(b"video")
        return DownloadResult(path, self.info, 5, workdir)


async def drain(service, rounds: int = 200) -> None:
    """Wait until the service has no tasks left (and threads are done)."""
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        if not service._tasks and service.active == 0 and not service.queued:
            return
    raise AssertionError("service did not settle")
