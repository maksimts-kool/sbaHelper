"""Download orchestration: admission, queue, deduplication, workers and delivery.

Lifecycle of one link (a `Request`):

1. `admit()` checks shutdown, the per-user rate, per-user/per-chat pending
   limits, queue capacity and free disk space, and reserves a slot in those
   counters (`Ticket`). Refusals are coded (`Refused.code`).
2. `submit()` looks the video up in the Telegram file cache (directly or via
   a remembered short-link alias). A hit is sent by `file_id` at once.
3. Otherwise the request joins a job for the same video that is already in
   flight, or a new `Job` is queued. Queues are per chat and served
   round-robin, so one busy chat cannot starve the others.
4. A job runs `download.download_video` in a dedicated thread pool of
   MAX_ACTIVE_DOWNLOADS threads. The worker cooperates with a deadline
   (`download.Cancel`). If it overruns anyway, waiters are told it timed out
   but the slot stays taken until the thread really ends, so capacity is
   never over-committed. After extraction the worker checks the cache again
   under the resolved video id (`Satisfied`), which catches short links to an
   already cached video.
5. Delivery: the first waiter gets an upload; its `file_id` is cached and the
   other waiters (and anybody who joins meanwhile) get the same file by id.
   If an upload fails, the next waiter tries uploading again, so one failure
   does not poison the rest. The job owns the work directory and deletes it
   once every waiter has been served, never earlier.

Waiters are not tasks: nothing a single requester does can cancel a job that
others still need. Jobs are tracked here (not with `Application.create_task`)
so `shutdown()` decides their fate: queued jobs are dropped with a "restart"
notice, running ones are asked to stop and given a grace period.

Limitation: two *different* unresolved short links to the same video that
arrive at the same time are downloaded twice (their identity is only known
after extraction); the cache serves every later request.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import secrets
import sqlite3
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from telegram import Message
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, TelegramError, TimedOut

from sbahelper import cookies, texts
from sbahelper.cache import CachedVideo, FileCache
from sbahelper.config import Settings
from sbahelper.download import (
    Cancel,
    Cancelled,
    DownloadError,
    DownloadResult,
    Rejected,
    Satisfied,
    VideoInfo,
    download_video,
    free_bytes,
    required_free_bytes,
)
from sbahelper.download import (
    TimedOut as DownloadTimedOut,
)
from sbahelper.links import Link
from sbahelper.pacing import LOW, Throttled
from sbahelper.prefs import Preferences
from sbahelper.stats import DownloadEvent, StatsStore

log = logging.getLogger(__name__)

# Rejections ("photo post", "too long") are shown briefly, then removed.
REJECTION_TTL_SEC = 5.0
# Edits of one status message are spaced at least this far apart.
MIN_EDIT_INTERVAL_SEC = 1.5
# Extra time a worker gets past its deadline before waiters are told it timed out.
OVERRUN_GRACE_SEC = 30.0
# `upload_video` lasts ~5 s on the client; refreshed while an upload runs.
CHAT_ACTION_EVERY_SEC = 4.5
_STALE_FILE_ID = (
    "file identifier",
    "wrong remote file",
    "file reference",
    "can't use file of type",
    "file_id",
)


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _rid() -> str:
    return secrets.token_hex(3)


# --------------------------------------------------------------------------- #
#  Status message                                                             #
# --------------------------------------------------------------------------- #


class StatusMessage:
    """The reply that follows one link: checking → title and progress → sending → gone.

    Progress edits are low priority: when the chat has no budget they are
    skipped (`Throttled`), never queued in front of the final result. Final
    texts are normal priority, and once one is shown no progress can replace it.
    """

    def __init__(self, message: Message) -> None:
        self.message = message
        self._lock = asyncio.Lock()
        self._shown = message.text or ""
        self._last_edit = 0.0
        self._info: VideoInfo | None = None
        self._percent = -1
        self._live = True
        self._pending: asyncio.Task | None = None

    def set_info(self, info: VideoInfo) -> None:
        self._info = info
        self._schedule()

    def set_progress(self, percent: float) -> None:
        step = int(max(0.0, min(percent, 100.0))) // 10 * 10
        if step > self._percent:
            self._percent = step
            self._schedule()

    def _schedule(self) -> None:
        """Coalesce: at most one pending refresh, which shows the latest state."""
        if self._live and (self._pending is None or self._pending.done()):
            self._pending = asyncio.get_running_loop().create_task(self._refresh())

    async def _refresh(self) -> None:
        wait = self._last_edit + MIN_EDIT_INTERVAL_SEC - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        async with self._lock:
            if not self._live or self._info is None:
                return
            line = texts.progress(self._percent) if self._percent >= 0 else texts.CHECKING
            await self._edit(texts.status(self._info, line), low=True)

    async def note(self, text: str) -> None:
        """An intermediate text (e.g. "queued"); later progress may replace it."""
        async with self._lock:
            if self._live:
                await self._edit(text, low=True)

    async def show(self, text: str) -> None:
        """Replace the status with a final text; progress updates stop."""
        self._live = False
        async with self._lock:
            await self._edit(text, low=False)

    async def flash(self, text: str, seconds: float) -> None:
        await self.show(text)
        await asyncio.sleep(seconds)
        await self.delete()

    async def delete(self, attempts: int = 3) -> None:
        self._live = False
        for attempt in range(1, attempts + 1):
            try:
                await self.message.delete()
                return
            except TelegramError as error:
                if "not found" in str(error).lower():
                    return
                if attempt == attempts:
                    log.warning("Status not deleted. Chat=%s Error=%s", self.message.chat_id, error)
                    return
                await asyncio.sleep(attempt)

    async def _edit(self, text: str, *, low: bool) -> None:
        if text == self._shown:
            return
        try:
            if low:
                await self.message.edit_text(text, rate_limit_args=LOW)
            else:
                await self.message.edit_text(text)
        except Throttled:
            return
        except TelegramError as error:
            if "not modified" not in str(error).lower():
                log.warning("Status not updated. Chat=%s Error=%s", self.message.chat_id, error)
                return
        self._shown = text
        self._last_edit = time.monotonic()


# --------------------------------------------------------------------------- #
#  Requests and jobs                                                          #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Ticket:
    """Reserved admission counters; released exactly once."""

    service: DownloadService
    chat_id: int
    user_id: int
    released: bool = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.service._release(self)


@dataclass(slots=True)
class Request:
    link: Link
    message: Message
    status: StatusMessage
    ticket: Ticket
    chat_id: int
    user_id: int
    user_name: str = ""
    username: str | None = None
    id: str = field(default_factory=_rid)

    def who(self) -> str:
        return f"Req={self.id} Chat={self.chat_id} User={self.user_id}"


@dataclass(slots=True, eq=False)  # identity semantics: jobs live in sets and dicts
class Job:
    key: str
    link: Link
    chat_id: int
    waiters: list[Request] = field(default_factory=list)
    cancel: Cancel = field(default_factory=Cancel)
    keys: set[str] = field(default_factory=set)
    state: str = "queued"
    info: VideoInfo | None = None
    percent: float = 0.0
    id: str = field(default_factory=_rid)


@dataclass(frozen=True, slots=True)
class PlatformStatus:
    has_cookies: bool
    cookie_health: cookies.Health | None
    last_check_ok: bool | None
    last_check_at: datetime | None


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    uptime_sec: int
    active: int
    queued: int
    max_active: int
    max_queued: int
    cache_hits: int
    cache_misses: int
    cache_invalidated: int
    cache_entries: int
    flood_waits: int
    throttled: int
    temp_free_bytes: int | None
    database_ok: bool | None
    access: str
    privacy_mode: str
    shutting_down: bool
    platforms: dict[str, PlatformStatus]


type Runner = Callable[..., DownloadResult]


class DownloadService:
    def __init__(
        self,
        settings: Settings,
        *,
        cache: FileCache | None = None,
        stats: StatsStore | None = None,
        prefs: Preferences | None = None,
        runner: Runner = download_video,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.stats = stats
        self.prefs = prefs
        self._runner = runner
        self._clock = clock
        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_active_downloads, thread_name_prefix="download"
        )
        self._inflight: dict[str, Job] = {}
        self._queues: OrderedDict[int, deque[Job]] = OrderedDict()
        self._running: set[Job] = set()
        # Worker threads still alive (a timed-out job keeps its slot until its thread ends).
        self._busy_threads = 0
        self._tasks: set[asyncio.Task] = set()
        self._pending_user: defaultdict[int, int] = defaultdict(int)
        self._pending_chat: defaultdict[int, int] = defaultdict(int)
        self._recent: defaultdict[int, deque[float]] = defaultdict(deque)
        self._closing = False
        self._started = clock()
        self.cache_hits = self.cache_misses = self.cache_invalidated = 0

    # -- admission ----------------------------------------------------------- #

    @property
    def queued(self) -> int:
        return sum(len(queue) for queue in self._queues.values())

    @property
    def active(self) -> int:
        return self._busy_threads

    @property
    def closing(self) -> bool:
        return self._closing

    def admit(self, chat_id: int, user_id: int) -> Ticket:
        """Reserve room for one link or raise `Refused` with a reason code."""
        s = self.settings
        if self._closing:
            raise Refused("shutting_down")
        now = self._clock()
        recent = self._recent[user_id]
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= s.user_links_per_minute:
            raise Refused("rate_limited")
        if self._pending_user[user_id] >= s.max_pending_per_user:
            raise Refused("user_busy")
        if self._pending_chat[chat_id] >= s.max_pending_per_chat:
            raise Refused("chat_busy")
        if self.queued >= s.max_queued_downloads and self.active >= s.max_active_downloads:
            raise Refused("busy")
        free = free_bytes(s.temp_dir)
        if free is not None and free < required_free_bytes():
            log.warning("Low temporary space. Free=%d Needed=%d", free, required_free_bytes())
            raise Refused("no_space")
        recent.append(now)
        self._pending_user[user_id] += 1
        self._pending_chat[chat_id] += 1
        return Ticket(self, chat_id, user_id)

    def _release(self, ticket: Ticket) -> None:
        for counter, key in (
            (self._pending_user, ticket.user_id),
            (self._pending_chat, ticket.chat_id),
        ):
            counter[key] -= 1
            if counter[key] <= 0:
                del counter[key]

    # -- submission ---------------------------------------------------------- #

    async def submit(self, request: Request, *, use_cache: bool = True) -> None:
        """Serve one admitted request: from cache, by joining a job, or as a new job."""
        key = request.link.key
        if use_cache and self.cache is not None and self.cache.enabled:
            try:
                video_key = (
                    key
                    if request.link.resolved
                    else await asyncio.to_thread(self.cache.resolve, key)
                )
                hit = await asyncio.to_thread(self.cache.get, video_key) if video_key else None
            except sqlite3.Error as error:
                log.error("Cache lookup failed. %s Error=%s", request.who(), error)
                video_key, hit = None, None
            if hit is not None:
                self.cache_hits += 1
                log.info("Cache hit. %s Video=%s", request.who(), hit.video_key)
                self._spawn(self._serve_cached(request, hit))
                return
            self.cache_misses += 1
            key = video_key or key

        job = self._inflight.get(key) or self._inflight.get(request.link.key)
        if job is not None and job.state in ("queued", "running"):
            job.waiters.append(request)
            log.info("Joined in-flight job. %s Job=%s", request.who(), job.id)
            if job.info is not None:
                request.status.set_info(job.info)
            return

        job = Job(key=key, link=request.link, chat_id=request.chat_id, waiters=[request])
        job.keys = {key, request.link.key}
        for k in job.keys:
            self._inflight[k] = job
        self._queues.setdefault(request.chat_id, deque()).append(job)
        log.info(
            "Job queued. %s Job=%s Video=%s Queue=%d",
            request.who(),
            job.id,
            key,
            self.queued,
        )
        if self.active >= self.settings.max_active_downloads:
            self._spawn(request.status.note(texts.QUEUED))
        self._dispatch()

    def _next_job(self) -> Job | None:
        while self._queues:
            chat_id, queue = next(iter(self._queues.items()))
            job = queue.popleft()
            del self._queues[chat_id]
            if queue:  # back of the line: round-robin between chats
                self._queues[chat_id] = queue
            return job
        return None

    def _dispatch(self) -> None:
        while not self._closing and self.active < self.settings.max_active_downloads:
            job = self._next_job()
            if job is None:
                return
            job.state = "running"
            self._running.add(job)
            self._busy_threads += 1
            self._spawn(self._run(job))

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- worker -------------------------------------------------------------- #

    def _work(self, job: Job, loop: asyncio.AbstractEventLoop) -> DownloadResult:
        """Runs in a worker thread. UI callbacks are handed to the event loop."""

        def on_info(info: VideoInfo) -> None:
            if self.cache is not None and self.cache.enabled and info.key:
                hit = self.cache.get(info.key)
                if hit is not None:
                    raise Satisfied(hit)
            loop.call_soon_threadsafe(self._broadcast_info, job, info)

        def on_progress(percent: float) -> None:
            loop.call_soon_threadsafe(self._broadcast_progress, job, percent)

        return self._runner(
            job.link.url, on_info=on_info, on_progress=on_progress, cancel=job.cancel
        )

    def _broadcast_info(self, job: Job, info: VideoInfo) -> None:
        job.info = info
        for request in job.waiters:
            request.status.set_info(info)

    def _broadcast_progress(self, job: Job, percent: float) -> None:
        job.percent = percent
        for request in job.waiters:
            request.status.set_progress(percent)

    def _thread_done(self, _future: object) -> None:
        self._busy_threads -= 1
        self._dispatch()

    async def _run(self, job: Job) -> None:
        loop = asyncio.get_running_loop()
        timeout = self.settings.download_timeout_sec
        job.cancel.deadline = time.monotonic() + timeout
        started = time.monotonic()
        log.info("Job started. Job=%s Video=%s Waiters=%d", job.id, job.key, len(job.waiters))
        future = loop.run_in_executor(self._executor, functools.partial(self._work, job, loop))
        future.add_done_callback(self._thread_done)
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout + OVERRUN_GRACE_SEC)
            except asyncio.CancelledError:
                job.cancel.cancel("shutdown")
                future.add_done_callback(_cleanup_orphan)
                raise
            except TimeoutError:
                job.cancel.cancel("overrun")
                future.add_done_callback(_cleanup_orphan)
                log.error(
                    "Worker overran its deadline; slot held until it stops. Job=%s Video=%s",
                    job.id,
                    job.key,
                )
                await self._fail(job, DownloadTimedOut("worker overran its deadline"))
                return
            except Satisfied as hit:
                job.state = "serving-cache"  # nobody joins now; new requests hit the cache
                cached: CachedVideo = hit.value  # type: ignore[assignment]
                self.cache_hits += 1
                log.info("Cache hit after resolving. Job=%s Video=%s", job.id, cached.video_key)
                self._remember_alias(job, cached.video_key)
                await self._finish_cached(job, cached)
                return
            except DownloadError as error:
                await self._fail(job, error)
                return
            except Exception:
                log.exception("Download crashed. Job=%s Video=%s", job.id, job.key)
                await self._fail(job, None)
                return
            log.info(
                "Job downloaded. Job=%s Video=%s Size=%d Time=%.1fs",
                job.id,
                result.info.key,
                result.size_bytes,
                time.monotonic() - started,
            )
            if result.info.key:
                self._remember_alias(job, result.info.key)
            await self._deliver_file(job, result)
        finally:
            self._close_job(job)

    def _close_job(self, job: Job) -> None:
        job.state = "done"
        self._running.discard(job)
        for key in job.keys:
            if self._inflight.get(key) is job:
                del self._inflight[key]

    def _remember_alias(self, job: Job, video_key: str) -> None:
        if self.cache is None or job.link.resolved:
            return
        try:
            self.cache.remember_alias(job.link.key, video_key)
        except sqlite3.Error as error:
            log.warning("Alias not saved. Job=%s Error=%s", job.id, error)

    async def _fail(self, job: Job, error: DownloadError | None) -> None:
        """Tell every waiter; log once per job at the level the error deserves."""
        job.state = "done"
        if error is None:
            text = texts.UNEXPECTED
        else:
            text = texts.error_text(error, self.settings.max_file_size_bytes)
            first = job.waiters[0] if job.waiters else None
            where = first.who() if first else ""
            if isinstance(error, Rejected):
                log.info("Link rejected. %s Job=%s Reason=%s", where, job.id, error.reason)
            elif error.code == "login":
                log.error(
                    "Cookies needed: the site asks to log in. %s URL=%s Error=%s. "
                    "Send fresh cookies to the bot in a private chat.",
                    where,
                    job.link.url,
                    error.reason,
                )
            else:
                level = logging.ERROR if error.alert else logging.WARNING
                log.log(
                    level,
                    "Download failed. %s Job=%s URL=%s Code=%s Error=%s",
                    where,
                    job.id,
                    job.link.url,
                    error.code,
                    error.reason,
                )
        for request in self._take_waiters(job):
            try:
                if isinstance(error, Rejected):
                    self._spawn(request.status.flash(text, REJECTION_TTL_SEC))
                else:
                    await request.status.show(text)
            finally:
                request.ticket.release()

    def _take_waiters(self, job: Job):
        """Yield waiters, including ones that join while earlier ones are served."""
        index = 0
        while index < len(job.waiters):
            yield job.waiters[index]
            index += 1

    # -- delivery ------------------------------------------------------------ #

    async def _deliver_file(self, job: Job, result: DownloadResult) -> None:
        sent: tuple[str, str] | None = None
        try:
            for request in self._take_waiters(job):
                try:
                    sent = await self._deliver_one(request, result, sent)
                except Exception:
                    log.exception("Delivery crashed. %s Job=%s", request.who(), job.id)
                    await request.status.show(texts.UNEXPECTED)
                finally:
                    request.ticket.release()
        finally:
            job.state = "done"
            result.cleanup()

    async def _deliver_one(
        self, request: Request, result: DownloadResult, sent: tuple[str, str] | None
    ) -> tuple[str, str] | None:
        """Send by file id when an earlier waiter's upload gave us one, else upload."""
        if sent is not None:
            try:
                ok = await self._send_cached(request, sent[0], sent[1], result.info)
            except Forbidden as error:
                await self._chat_gone(request, error)
                return sent
            except TelegramError as error:
                log.warning("Video not sent. %s Error=%s", request.who(), error)
                await request.status.show(texts.send_failed(str(error)))
                return sent
            if ok:
                await self._delivered(request, result.info, result.size_bytes)
                return sent
        return await self._upload(request, result) or sent

    async def _upload(self, request: Request, result: DownloadResult) -> tuple[str, str] | None:
        info = result.info
        await request.status.show(texts.status(info, texts.SENDING))
        action = self._spawn(self._keep_uploading_action(request.message))
        try:
            with result.path.open("rb") as handle:
                message = await request.message.reply_video(
                    handle,
                    caption=texts.caption(info),
                    duration=info.duration or None,
                    width=info.width,
                    height=info.height,
                    supports_streaming=True,
                    do_quote=self._reply_to(request),
                )
        except TimedOut as error:
            # Telegram may have accepted the upload; retrying could post it twice.
            log.warning("Upload timed out; it may still arrive. %s Error=%s", request.who(), error)
            await request.status.show(texts.UPLOAD_UNCERTAIN)
            return None
        except Forbidden as error:
            await self._chat_gone(request, error)
            return None
        except TelegramError as error:
            log.warning("Video not sent. %s Error=%s", request.who(), error)
            await request.status.show(texts.send_failed(str(error)))
            return None
        except OSError as error:
            log.error("Video file unreadable. %s Error=%s", request.who(), error)
            await request.status.show(texts.UNEXPECTED)
            return None
        finally:
            action.cancel()

        file_ref = self._file_ref(message)
        if file_ref and info.key and self.cache is not None:
            try:
                await asyncio.to_thread(
                    self.cache.put, info.key, file_ref[0], file_ref[1], info, result.size_bytes
                )
            except sqlite3.Error as error:
                log.warning("Cache entry not saved. %s Error=%s", request.who(), error)
        await self._delivered(request, info, result.size_bytes)
        return file_ref

    async def _keep_uploading_action(self, message: Message) -> None:
        """`upload_video` while a long upload runs; skipped when the chat is busy."""
        try:
            while True:
                with suppress(TelegramError):
                    await message.reply_chat_action(ChatAction.UPLOAD_VIDEO, rate_limit_args=LOW)
                await asyncio.sleep(CHAT_ACTION_EVERY_SEC)
        except asyncio.CancelledError:
            return

    @staticmethod
    def _file_ref(message: Message | None) -> tuple[str, str] | None:
        if message is None:
            return None
        for kind in ("video", "animation", "document"):
            media = getattr(message, kind, None)
            if media is not None and getattr(media, "file_id", None):
                return media.file_id, kind
        return None

    @staticmethod
    def _reply_to(request: Request) -> Any:
        return request.message.build_reply_arguments(allow_sending_without_reply=True)

    async def _send_cached(
        self, request: Request, file_id: str, kind: str, info: VideoInfo
    ) -> bool:
        """Send by `file_id`. False when Telegram rejects the id (caller falls back)."""
        kwargs: dict[str, Any] = {
            "caption": texts.caption(info),
            "do_quote": self._reply_to(request),
        }
        try:
            if kind == "video":
                await request.message.reply_video(
                    file_id,
                    duration=info.duration or None,
                    width=info.width,
                    height=info.height,
                    supports_streaming=True,
                    **kwargs,
                )
            elif kind == "animation":
                await request.message.reply_animation(
                    file_id,
                    duration=info.duration or None,
                    width=info.width,
                    height=info.height,
                    **kwargs,
                )
            else:
                await request.message.reply_document(file_id, **kwargs)
        except BadRequest as error:
            if any(token in str(error).lower() for token in _STALE_FILE_ID):
                return False
            raise
        return True

    async def _serve_cached(self, request: Request, hit: CachedVideo) -> None:
        try:
            await request.status.show(texts.status(hit.info, texts.SENDING))
            try:
                ok = await self._send_cached(request, hit.file_id, hit.media_kind, hit.info)
            except Forbidden as error:
                await self._chat_gone(request, error)
                request.ticket.release()
                return
            except TelegramError as error:
                log.warning("Cached video not sent. %s Error=%s", request.who(), error)
                await request.status.show(texts.send_failed(str(error)))
                request.ticket.release()
                return
            if not ok:
                self.cache_invalidated += 1
                log.warning(
                    "Cached file rejected by Telegram; downloading again. %s Video=%s",
                    request.who(),
                    hit.video_key,
                )
                if self.cache is not None:
                    await asyncio.to_thread(self.cache.invalidate, hit.video_key)
                # One fresh attempt; the cache is bypassed so this cannot loop.
                await self.submit(request, use_cache=False)
                return
            if self.cache is not None:
                await asyncio.to_thread(self.cache.touch, hit.video_key)
            await self._delivered(request, hit.info, hit.size_bytes)
            request.ticket.release()
        except BaseException:
            request.ticket.release()
            raise

    async def _finish_cached(self, job: Job, hit: CachedVideo) -> None:
        """The worker found the video cached after resolving a short link."""
        for request in self._take_waiters(job):
            await self._serve_cached(request, hit)

    async def _delivered(self, request: Request, info: VideoInfo, size: int) -> None:
        log.info(
            "Video sent. %s Platform=%s Video=%s Size=%d",
            request.who(),
            info.platform,
            info.key,
            size,
        )
        await request.status.delete()
        if self.stats is None:
            return
        event = DownloadEvent(
            chat_id=request.chat_id,
            user_id=request.user_id,
            user_name=request.user_name,
            username=request.username,
            platform=info.platform,
            duration_sec=info.duration,
            size_bytes=size,
            title=info.title,
            uploader=info.uploader,
            view_count=info.view_count,
        )
        try:
            await asyncio.to_thread(self.stats.record, event)
        except (OSError, sqlite3.Error) as error:
            log.error("Download not recorded. %s Error=%s", request.who(), error)

    async def _chat_gone(self, request: Request, error: TelegramError) -> None:
        """The bot was blocked or removed: stop scheduled messages to that chat."""
        log.warning("Chat unreachable. %s Error=%s", request.who(), error)
        if self.prefs is not None:
            try:
                await asyncio.to_thread(self.prefs.update_chat, request.chat_id, active=False)
            except sqlite3.Error as db_error:
                log.warning("Chat not marked inactive. %s Error=%s", request.who(), db_error)

    # -- shutdown and status -------------------------------------------------- #

    async def shutdown(self, grace_sec: float = 20.0) -> None:
        """Stop accepting, drop queued jobs with a notice, let running ones finish."""
        if self._closing:
            return
        self._closing = True
        queued = [job for queue in self._queues.values() for job in queue]
        self._queues.clear()
        log.info(
            "Download service stopping. Running=%d Queued=%d Grace=%.0fs",
            len(self._running),
            len(queued),
            grace_sec,
        )
        for job in queued:
            self._close_job(job)
            await self._fail(job, Cancelled("shutdown"))
        for job in list(self._running):
            job.cancel.cancel("shutdown")
        if self._tasks:
            _, pending = await asyncio.wait(set(self._tasks), timeout=grace_sec)
            for task in pending:
                task.cancel()
            if pending:
                log.warning("Tasks cancelled at shutdown. Count=%d", len(pending))
                await asyncio.wait(pending, timeout=2)
        self._executor.shutdown(wait=False, cancel_futures=True)
        log.info("Download service stopped.")

    def status(
        self,
        *,
        database_ok: bool | None,
        privacy_mode: str,
        flood_waits: int = 0,
        throttled: int = 0,
        last_checks: dict[str, tuple[bool, datetime]] | None = None,
    ) -> ServiceStatus:
        platforms = {}
        for platform in cookies.PLATFORM_DOMAINS:
            check = (last_checks or {}).get(platform)
            platforms[platform] = PlatformStatus(
                has_cookies=cookies.platform_file(self.settings.cookies_dir, platform).is_file(),
                cookie_health=cookies.health(platform),
                last_check_ok=check[0] if check else None,
                last_check_at=check[1] if check else None,
            )
        entries = 0
        if self.cache is not None:
            try:
                entries = self.cache.count()
            except sqlite3.Error:
                entries = 0
        return ServiceStatus(
            uptime_sec=int(self._clock() - self._started),
            active=self.active,
            queued=self.queued,
            max_active=self.settings.max_active_downloads,
            max_queued=self.settings.max_queued_downloads,
            cache_hits=self.cache_hits,
            cache_misses=self.cache_misses,
            cache_invalidated=self.cache_invalidated,
            cache_entries=entries,
            flood_waits=flood_waits,
            throttled=throttled,
            temp_free_bytes=free_bytes(self.settings.temp_dir),
            database_ok=database_ok,
            access=self.settings.describe_access(),
            privacy_mode=privacy_mode,
            shutting_down=self._closing,
            platforms=platforms,
        )


def utcnow() -> datetime:
    return datetime.now(UTC)


def _cleanup_orphan(future: asyncio.Future) -> None:
    """A worker nobody waits for any more finished: remove what it downloaded."""
    if future.cancelled() or future.exception() is not None:
        return
    result = future.result()
    if isinstance(result, DownloadResult):
        result.cleanup()
