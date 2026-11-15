"""Error alerts: every ERROR log record is forwarded to the admins' Telegram DMs.

Alerts are grouped by their message with ids, numbers and URLs masked, and each
group is sent at most once per `COOLDOWN_SEC`, so a failure that repeats for
every link does not flood the admins. The text is redacted (no tokens, cookies
or signed URLs) and kept well under Telegram's 4096-character message limit.
Records logged while an alert is being sent never produce another alert.
"""

from __future__ import annotations

import asyncio
import contextvars
import html
import logging
import re
import time
from collections.abc import Iterable
from contextlib import suppress

from telegram import Bot
from telegram.error import TelegramError

from sbahelper.log import tag_for
from sbahelper.redact import redact

log = logging.getLogger(__name__)

COOLDOWN_SEC = 600
# Visible characters; Telegram allows 4096 per message after entity parsing.
_MESSAGE_LIMIT = 800
_TRACEBACK_LIMIT = 2500
# Bounds for the bookkeeping: distinct alert groups remembered, sends in flight.
MAX_KEYS = 500
MAX_PENDING = 20
_VOLATILE = re.compile(r"https?://\S+|-?\d+(?:\.\d+)?")
# Set inside the task that sends alerts: anything it logs is not alerted again.
_sending = contextvars.ContextVar("sending_alert", default=False)


def alert_key(record: logging.LogRecord) -> str:
    """What counts as "the same alert": logger + message without ids, sizes and URLs."""
    return f"{record.name}:{_VOLATILE.sub('#', record.getMessage())}"


def _clip(text: str, limit: int, *, keep_end: bool = False) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1) :] if keep_end else text[: limit - 1] + "…"


def render_alert(record: logging.LogRecord, formatter: logging.Formatter) -> str:
    """Plain text is clipped and redacted *before* escaping, so no entity is cut."""
    message = _clip(redact(record.getMessage()), _MESSAGE_LIMIT)
    lines = [
        f"🚨 <b>{record.levelname}</b> · {tag_for(record.name)}",
        f"<code>{html.escape(message)}</code>",
    ]
    if record.exc_info:
        traceback = redact(formatter.formatException(record.exc_info))
        lines.append(f"<pre>{html.escape(_clip(traceback, _TRACEBACK_LIMIT, keep_end=True))}</pre>")
    return "\n".join(lines)


class TelegramAlerts(logging.Handler):
    """Logging handler that DMs ERROR records to admins.

    `emit` may run on any thread (downloads run in workers), so sending is
    handed over to the bot's event loop. `logging.Handler.handle` already holds
    `self.lock` around `emit`, which keeps the cooldown bookkeeping consistent.
    """

    def __init__(self, bot: Bot, admin_ids: Iterable[int], loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(logging.ERROR)
        self.setFormatter(logging.Formatter())
        self._bot = bot
        self._admin_ids = tuple(admin_ids)
        self._loop = loop
        self._last_sent: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        if _sending.get() or record.name == __name__:
            return
        key = alert_key(record)
        now = time.monotonic()
        if now - self._last_sent.get(key, -COOLDOWN_SEC) < COOLDOWN_SEC:
            return
        self._remember(key, now)
        if len(self._tasks) >= MAX_PENDING:
            self.dropped += 1
            return

        text = render_alert(record, self.formatter or logging.Formatter())
        # A closed loop means the bot is shutting down; the alert is dropped.
        with suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._spawn, text)

    def _remember(self, key: str, now: float) -> None:
        if len(self._last_sent) >= MAX_KEYS:
            expired = [k for k, at in self._last_sent.items() if now - at >= COOLDOWN_SEC]
            for stale in expired or sorted(self._last_sent, key=self._last_sent.get)[:50]:
                del self._last_sent[stale]
        self._last_sent[key] = now

    def _spawn(self, text: str) -> None:
        if len(self._tasks) >= MAX_PENDING:
            self.dropped += 1
            return
        task = self._loop.create_task(self._send(text), context=contextvars.copy_context())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, text: str) -> None:
        _sending.set(True)
        for admin_id in self._admin_ids:
            try:
                await self._bot.send_message(admin_id, text, parse_mode="HTML")
            except TelegramError as error:
                # WARNING, not ERROR: a failed alert must not trigger another alert.
                log.warning("Alert not delivered. Admin=%s Error=%s", admin_id, error)

    async def aclose(self, timeout: float = 5.0) -> None:
        """Give alerts already in flight a moment to go out (used at shutdown)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)
