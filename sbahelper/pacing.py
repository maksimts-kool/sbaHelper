"""Outbound pacing for every Bot API call (a python-telegram-bot rate limiter).

Telegram's FAQ asks bots to stay under roughly one message per second per chat,
20 messages per minute per group and about 30 messages per second overall
(https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).
Those are guidance, not guarantees, so on top of pacing every call honours the
`retry_after` Telegram returns with a flood-control error
(https://core.telegram.org/bots/api#responseparameters).

Two priorities:

- normal (default): waits for its turn and is retried after `RetryAfter`.
- LOW (progress edits, chat actions): never waits. When the chat has no budget
  left or is in flood cooldown the call raises `Throttled` and the caller just
  skips that update, so intermediate progress never delays a final result.

Only flood control is retried. A timeout may happen after Telegram accepted
the message, so retrying it could post the video twice; timeouts propagate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from telegram.error import RetryAfter, TelegramError
from telegram.ext import BaseRateLimiter

log = logging.getLogger(__name__)

LOW = {"priority": "low"}

# Calls that do not post anything into a chat: only the global budget applies.
_UNCOUNTED = frozenset(
    {
        "answerCallbackQuery",
        "deleteMessage",
        "deleteMessages",
        "getChat",
        "getChatMember",
        "getFile",
        "getMe",
        "getMyCommands",
        "setMyCommands",
        "deleteMyCommands",
        "sendChatAction",
    }
)


class Throttled(TelegramError):
    """A low-priority call was skipped because the chat has no budget right now."""

    def __init__(self, message: str = "Skipped: chat is rate limited") -> None:
        super().__init__(message)


def retry_after_seconds(error: RetryAfter) -> float:
    """`RetryAfter.retry_after` is an int or a timedelta depending on PTB settings."""
    value = error._retry_after if hasattr(error, "_retry_after") else error.retry_after
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


@dataclass(slots=True)
class Bucket:
    """Token bucket: `capacity` calls at once, refilled at `rate` per second."""

    rate: float
    capacity: float
    tokens: float = -1.0
    updated: float = field(default_factory=time.monotonic)
    # Flood-control cooldown reported by Telegram for this bucket.
    blocked_until: float = 0.0

    def __post_init__(self) -> None:
        if self.tokens < 0:
            self.tokens = self.capacity

    def _refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def wait_time(self, now: float) -> float:
        """Seconds until one token is available (0 = now)."""
        self._refill(now)
        blocked = max(0.0, self.blocked_until - now)
        missing = max(0.0, 1.0 - self.tokens) / self.rate
        return max(blocked, missing)

    def take(self, now: float) -> None:
        self._refill(now)
        self.tokens -= 1.0


class Pacer(BaseRateLimiter[dict]):
    """Per-chat and global token buckets plus bounded `RetryAfter` retries."""

    def __init__(
        self,
        *,
        overall_per_sec: float = 25.0,
        group_per_min: float = 20.0,
        private_per_sec: float = 1.0,
        max_retries: int = 2,
        max_wait_sec: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._overall = Bucket(overall_per_sec, overall_per_sec, updated=clock())
        self._group_rate = group_per_min / 60.0
        self._private_rate = private_per_sec
        self._chats: dict[int | str, Bucket] = {}
        self._max_retries = max_retries
        self._max_wait = max_wait_sec
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self.flood_waits = 0
        self.skipped = 0

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        self._chats.clear()

    def _chat_bucket(self, chat_id: int | str) -> Bucket:
        bucket = self._chats.get(chat_id)
        if bucket is None:
            now = self._clock()
            is_group = isinstance(chat_id, str) or chat_id < 0
            # Small bursts are fine (a reply right after a command); sustained
            # traffic settles at the documented rate.
            bucket = (
                Bucket(self._group_rate, 5.0, updated=now)
                if is_group
                else Bucket(self._private_rate, 3.0, updated=now)
            )
            if len(self._chats) > 5000:  # forget idle chats
                for key in [k for k, b in self._chats.items() if b.wait_time(now) == 0][:1000]:
                    del self._chats[key]
            self._chats[chat_id] = bucket
        return bucket

    async def _acquire(self, buckets: list[Bucket], low: bool) -> None:
        deadline = self._clock() + self._max_wait
        while True:
            async with self._lock:
                now = self._clock()
                wait = max(bucket.wait_time(now) for bucket in buckets)
                if wait <= 0:
                    for bucket in buckets:
                        bucket.take(now)
                    return
            if low:
                self.skipped += 1
                raise Throttled
            if now + wait > deadline:
                raise RetryAfter(int(wait) + 1)
            await self._sleep(min(wait, 5.0))

    async def process_request(
        self,
        callback: Callable[..., Coroutine[Any, Any, bool | dict | list[dict]]],
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: dict | None,
    ) -> bool | dict | list[dict]:
        low = bool(rate_limit_args and rate_limit_args.get("priority") == "low")
        chat_id = data.get("chat_id")
        counted = chat_id is not None and endpoint not in _UNCOUNTED
        buckets = [self._overall]
        if counted:
            buckets.append(self._chat_bucket(chat_id))
        elif chat_id is not None and endpoint in _UNCOUNTED:
            # Chat actions and deletions do not spend the chat's budget, but they
            # still respect a flood cooldown Telegram set for that chat.
            cooldown = self._chat_bucket(chat_id).blocked_until - self._clock()
            if cooldown > 0:
                if low:
                    self.skipped += 1
                    raise Throttled
                await self._sleep(cooldown)

        attempt = 0
        while True:
            await self._acquire(buckets, low)
            try:
                return await callback(*args, **kwargs)
            except RetryAfter as error:
                delay = retry_after_seconds(error)
                self.flood_waits += 1
                target = buckets[-1] if counted else self._overall
                target.blocked_until = max(target.blocked_until, self._clock() + delay)
                if low or attempt >= self._max_retries:
                    log.warning(
                        "Flood control. Endpoint=%s Chat=%s RetryAfter=%.0fs Gave up",
                        endpoint,
                        chat_id,
                        delay,
                    )
                    if low:
                        raise Throttled from error
                    raise
                attempt += 1
                log.warning(
                    "Flood control. Endpoint=%s Chat=%s RetryAfter=%.0fs Attempt=%d/%d",
                    endpoint,
                    chat_id,
                    delay,
                    attempt,
                    self._max_retries,
                )
                # Cancellation (shutdown) interrupts this sleep like any other.
                await self._sleep(delay + 0.5)
