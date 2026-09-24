import logging
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sbahelper.log import PrettyFormatter, setup_logging, tag_for


def record(name: str = "sbahelper.download", message: str = "Downloaded. Size=%s", *args):
    return logging.LogRecord(name, logging.INFO, __file__, 1, message, args or ("3MB",), None)


@pytest.mark.parametrize(
    ("logger", "tag"),
    [
        ("sbahelper.download", "DOWNLOAD"),
        ("sbahelper.handlers", "BOT"),
        ("sbahelper.app", "BOT"),
        ("telegram.ext.Application", "TELEGRAM"),
        ("httpx", "HTTP"),
        ("apscheduler.scheduler", "SCHEDULER"),
        ("sbahelper", "SBAHELPER"),
    ],
)
def test_tags_come_from_logger_names(logger: str, tag: str) -> None:
    assert tag_for(logger) == tag


def test_line_matches_the_house_format() -> None:
    item = record()
    item.created = datetime(2026, 9, 23, 0, 7, 6, tzinfo=ZoneInfo("Europe/Tallinn")).timestamp()

    line = PrettyFormatter(ZoneInfo("Europe/Tallinn")).format(item)

    assert line == "2026-09-23T00:07:06+03:00 [DOWNLOAD] INFO: Downloaded. Size=3MB"


def test_local_time_is_used_without_a_timezone() -> None:
    line = PrettyFormatter().format(record())
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d \[DOWNLOAD] INFO: ", line)


def test_traceback_follows_the_line() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        item = record()
        item.exc_info = sys.exc_info()

    lines = PrettyFormatter().format(item).splitlines()

    assert lines[0].endswith("INFO: Downloaded. Size=3MB")
    assert lines[-1] == "ValueError: boom"


def test_colour_is_optional() -> None:
    assert "\x1b[" not in PrettyFormatter().format(record())
    assert "\x1b[32mINFO:" in PrettyFormatter(color=True).format(record())


def test_setup_replaces_handlers_and_quiets_chatty_libraries() -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        setup_logging("DEBUG")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, PrettyFormatter)
        assert root.level == logging.DEBUG
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)
