"""Pretty one-line logs.

    2026-09-23T00:07:06+03:00 [DOWNLOAD] INFO: Downloaded. Platform=tiktok Size=3.1MB Time=2.4s

The tag comes from the logger name (`sbahelper.download` → DOWNLOAD). Messages
read as a short sentence followed by `Key=Value` pairs. Colours are used only
when writing to a terminal (or with FORCE_COLOR) and never with NO_COLOR.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, tzinfo

_TAG_ALIASES = {
    "app": "BOT",
    "handlers": "BOT",
    "jobs": "JOBS",
    "httpx": "HTTP",
    "httpcore": "HTTP",
    "apscheduler": "SCHEDULER",
}
_QUIET_LOGGERS = ("httpx", "httpcore", "apscheduler")

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_TAG_COLOR = "\x1b[36m"
_LEVEL_COLORS = {
    "DEBUG": "\x1b[2m",
    "INFO": "\x1b[32m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[1;31m",
}


def tag_for(logger_name: str) -> str:
    """`sbahelper.cookies` → COOKIES, `telegram.ext.Application` → TELEGRAM."""
    parts = logger_name.split(".")
    key = parts[1] if parts[0] == "sbahelper" and len(parts) > 1 else parts[0]
    return _TAG_ALIASES.get(key, key).upper()


class PrettyFormatter(logging.Formatter):
    def __init__(self, tz: tzinfo | None = None, *, color: bool = False) -> None:
        super().__init__()
        self.tz = tz
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, self.tz).astimezone(self.tz)
        parts = [
            stamp.isoformat(timespec="seconds"),
            f"[{tag_for(record.name)}]",
            f"{record.levelname}:",
            record.getMessage(),
        ]
        if self.color:
            parts[0] = f"{_DIM}{parts[0]}{_RESET}"
            parts[1] = f"{_TAG_COLOR}{parts[1]}{_RESET}"
            parts[2] = f"{_LEVEL_COLORS.get(record.levelname, '')}{parts[2]}{_RESET}"

        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line


def _wants_color(stream) -> bool:
    if os.getenv("NO_COLOR"):
        return False
    return bool(os.getenv("FORCE_COLOR")) or (hasattr(stream, "isatty") and stream.isatty())


def setup_logging(level: str = "INFO", tz: tzinfo | None = None) -> None:
    """Route every logger through one pretty handler on stderr."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(PrettyFormatter(tz, color=_wants_color(handler.stream)))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
