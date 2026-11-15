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


# --------------------------------------------------------------------------- #
#  Redaction                                                                  #
# --------------------------------------------------------------------------- #

from sbahelper.redact import redact, safe_url  # noqa: E402

TOKEN = "123456789:AAHf9kQ2wZ_x-yLmNoPqRsTuVwXyZ012345"


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        (f"POST https://api.telegram.org/bot{TOKEN}/getUpdates", "AAHf9kQ2"),
        (f"token is {TOKEN}", "AAHf9kQ2"),
        ("Authorization: Bearer abc.def.ghi", "abc.def"),
        ("Cookie: sessionid=s3cr3t; sid_tt=xyz", "s3cr3t"),
        ("{'sessionid': 'zzz123'}", "zzz123"),
        ("https://rr3.googlevideo.com/videoplayback?expire=1&sig=SIGNED&ip=1.2.3.4", "SIGNED"),
        ("https://v16.tiktokcdn.com/a/b.mp4?x-expires=9&x-signature=SIGNED", "SIGNED"),
        ("https://user:pw@www.youtube.com/watch?v=abcdefghijk", "pw"),
    ],
)
def test_secrets_are_masked(raw: str, secret: str) -> None:
    assert secret not in redact(raw)


def test_video_identity_survives_redaction() -> None:
    url = "https://www.youtube.com/watch?v=abcdefghijk&si=tracking"
    assert safe_url(url) == "https://www.youtube.com/watch?v=abcdefghijk&si=***"
    assert "123" in redact("https://www.tiktok.com/@u/video/123?_t=x")


def test_formatter_redacts_messages_and_tracebacks() -> None:
    try:
        raise RuntimeError(f"failed for {TOKEN}")
    except RuntimeError:
        item = record("sbahelper.download", "Request to https://api.telegram.org/bot%s/x", TOKEN)
        item.exc_info = sys.exc_info()
    line = PrettyFormatter().format(item)
    assert "AAHf9kQ2" not in line and "123456789:***" in line
