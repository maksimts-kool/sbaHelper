"""The long-lived objects of one running bot, shared by handlers and jobs.

Built once in `app.post_init` and stored in `bot_data[RUNTIME_KEY]`; nothing
here is a module-level global. Each part is optional: without a database the
bot still downloads, it just has no statistics, cache or settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sbahelper.cache import FileCache
from sbahelper.db import Database
from sbahelper.pacing import Pacer
from sbahelper.prefs import Preferences
from sbahelper.service import DownloadService
from sbahelper.stats import StatsStore

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

RUNTIME_KEY = "runtime"


@dataclass(slots=True)
class Runtime:
    service: DownloadService
    db: Database | None = None
    stats: StatsStore | None = None
    cache: FileCache | None = None
    prefs: Preferences | None = None
    pacer: Pacer | None = None
    # getMe: can the bot read every group message (Privacy Mode off)?
    reads_all_group_messages: bool | None = None
    extras: dict[str, object] = field(default_factory=dict)

    def privacy_mode(self) -> str:
        if self.reads_all_group_messages is None:
            return "неизвестно"
        if self.reads_all_group_messages:
            return "выключен — вижу все сообщения в группах"
        return "включён — в группах вижу команды, ответы мне и всё, где я администратор"


def runtime(context: ContextTypes.DEFAULT_TYPE) -> Runtime | None:
    value = context.bot_data.get(RUNTIME_KEY)
    return value if isinstance(value, Runtime) else None
