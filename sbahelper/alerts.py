"""Error alerts: every ERROR log record is forwarded to the admins' Telegram DMs.

This replaces Sentry. Alerts are grouped by their message with ids, numbers and
URLs masked, and each group is sent at most once per `COOLDOWN_SEC`, so a
failure that repeats for every link does not flood the admins.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import Iterable
from contextlib import suppress

from telegram import Bot
from telegram.error import TelegramError

from sbahelper.log import tag_for

log = logging.getLogger(__name__)

COOLDOWN_SEC = 600
_MESSAGE_LIMIT = 800
_TRACEBACK_LIMIT = 2500
_VOLATILE = re.compile(r"https?://\S+|-?\d+(?:\.\d+)?")


def alert_key(record: logging.LogRecord) -> str:
    """What counts as "the same alert": logger + message without ids, sizes and URLs."""
    return f"{record.name}:{_VOLATILE.sub('#', record.getMessage())}"


def render_alert(record: logging.LogRecord, formatter: logging.Formatter) -> str:
    message = record.getMessage()[:_MESSAGE_LIMIT]
    lines = [
        f"🚨 <b>{record.levelname}</b> · {tag_for(record.name)}",
        f"<code>{html.escape(message)}</code>",
    ]
    if record.exc_info:
        traceback = formatter.formatException(record.exc_info)[-_TRACEBACK_LIMIT:]
        lines.append(f"<pre>{html.escape(traceback)}</pre>")
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

    def emit(self, record: logging.LogRecord) -> None:
        key = alert_key(record)
        now = time.monotonic()
        if now - self._last_sent.get(key, -COOLDOWN_SEC) < COOLDOWN_SEC:
            return
        self._last_sent[key] = now

        text = render_alert(record, self.formatter or logging.Formatter())
        # A closed loop means the bot is shutting down; the alert is dropped.
        with suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._spawn, text)

    def _spawn(self, text: str) -> None:
        task = self._loop.create_task(self._send(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, text: str) -> None:
        for admin_id in self._admin_ids:
            try:
                await self._bot.send_message(admin_id, text, parse_mode="HTML")
            except TelegramError as error:
                # WARNING, not ERROR: a failed alert must not trigger another alert.
                log.warning("Alert not delivered. Admin=%s Error=%s", admin_id, error)
