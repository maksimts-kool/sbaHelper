import asyncio
import logging
import sys

import pytest
from telegram.error import TelegramError

from sbahelper import alerts
from sbahelper.alerts import TelegramAlerts, alert_key, render_alert


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> None:
        if self.fail:
            raise TelegramError("Forbidden: bot can't initiate conversation with a user")
        self.sent.append((chat_id, text))


def error_record(message: str, *args, name: str = "sbahelper.handlers") -> logging.LogRecord:
    return logging.LogRecord(name, logging.ERROR, __file__, 1, message, args, None)


def test_key_ignores_ids_sizes_and_urls() -> None:
    first = error_record("Failed. Chat=%s URL=%s Size=%s", -100, "https://a.b/1", 1.5)
    second = error_record("Failed. Chat=%s URL=%s Size=%s", -200, "https://c.d/2", 7.0)
    other = error_record("Different failure. Chat=%s", -100)

    assert alert_key(first) == alert_key(second)
    assert alert_key(first) != alert_key(other)


def test_alert_is_escaped_and_carries_the_traceback() -> None:
    try:
        raise RuntimeError("<boom>")
    except RuntimeError:
        item = error_record("Crashed on <b>%s</b>", "x & y")
        item.exc_info = sys.exc_info()

    text = render_alert(item, logging.Formatter())

    assert text.startswith("🚨 <b>ERROR</b> · BOT")
    assert "<code>Crashed on &lt;b&gt;x &amp; y&lt;/b&gt;</code>" in text
    assert "RuntimeError: &lt;boom&gt;</pre>" in text


def run_handler(bot: FakeBot, records: list[logging.LogRecord], admins=(1, 2), between=None):
    """Log `records` through a logger (which applies the level) from a worker thread."""
    logger = logging.getLogger("tests.alerts")
    logger.propagate = False

    async def scenario() -> None:
        handler = TelegramAlerts(bot, admins, asyncio.get_running_loop())
        logger.addHandler(handler)
        try:
            for item in records:
                await asyncio.to_thread(logger.handle, item)
                if between:
                    between(handler)
            await asyncio.sleep(0)
            await asyncio.gather(*handler._tasks)
        finally:
            logger.removeHandler(handler)

    asyncio.run(scenario())


def test_every_admin_gets_the_alert() -> None:
    bot = FakeBot()
    run_handler(bot, [error_record("Check failed. Platform=%s", "tiktok")])
    assert [chat for chat, _ in bot.sent] == [1, 2]


def test_repeats_are_suppressed_during_the_cooldown() -> None:
    bot = FakeBot()
    records = [
        error_record("Download failed. Chat=%s", -100),
        error_record("Download failed. Chat=%s", -200),
        error_record("Cookies need attention"),
    ]
    run_handler(bot, records, admins=(1,))
    assert len(bot.sent) == 2


def test_the_same_alert_is_sent_again_after_the_cooldown() -> None:
    def cooldown_passes(handler: TelegramAlerts) -> None:
        for key in handler._last_sent:
            handler._last_sent[key] -= alerts.COOLDOWN_SEC + 1

    bot = FakeBot()
    run_handler(bot, [error_record("Same"), error_record("Same")], (1,), cooldown_passes)
    assert len(bot.sent) == 2


def test_undeliverable_alert_only_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="sbahelper.alerts"):
        run_handler(FakeBot(fail=True), [error_record("Boom")], admins=(1,))
    assert [r.levelname for r in caplog.records if r.name == "sbahelper.alerts"] == ["WARNING"]


def test_lower_levels_are_ignored() -> None:
    bot = FakeBot()
    warning = error_record("Just a warning")
    warning.levelno, warning.levelname = logging.WARNING, "WARNING"
    run_handler(bot, [warning])
    assert bot.sent == []


def test_alert_text_is_redacted_and_bounded() -> None:
    from sbahelper import texts

    token = "123456789:AAHf9kQ2wZ_x-yLmNoPqRsTuVwXyZ012345"
    try:
        raise RuntimeError("x" * 10_000 + token)
    except RuntimeError:
        item = error_record("Failed with %s %s", token, "<" * 5000)
        item.exc_info = sys.exc_info()
    text = render_alert(item, logging.Formatter())
    assert "AAHf9kQ2" not in text
    assert texts.visible_length(text) <= texts.MESSAGE_LIMIT
    assert "&l…" not in text  # clipped before escaping, never inside an entity


def test_alerts_raised_while_sending_alerts_are_ignored() -> None:
    logger = logging.getLogger("tests.alerts.recursive")
    logger.propagate = False

    class LoggingBot(FakeBot):
        async def send_message(self, chat_id, text, parse_mode=None):
            logger.error("Something failed while sending an alert")
            await super().send_message(chat_id, text, parse_mode)

    bot = LoggingBot()

    async def scenario() -> None:
        handler = TelegramAlerts(bot, (1,), asyncio.get_running_loop())
        logger.addHandler(handler)
        try:
            logger.error("First failure")
            await asyncio.sleep(0)
            await asyncio.gather(*handler._tasks)
            await asyncio.sleep(0)
            assert not handler._tasks
        finally:
            logger.removeHandler(handler)

    asyncio.run(scenario())
    assert len(bot.sent) == 1


def test_alert_bookkeeping_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(alerts, "MAX_KEYS", 10)
    monkeypatch.setattr(alerts, "MAX_PENDING", 3)

    async def scenario() -> TelegramAlerts:
        handler = TelegramAlerts(FakeBot(), (1,), asyncio.get_running_loop())
        for index in range(50):
            handler.handle(error_record(f"Distinct failure {chr(65 + index % 26)}{index // 26}x"))
        await asyncio.sleep(0)
        assert len(handler._tasks) <= 3
        await asyncio.gather(*handler._tasks)
        return handler

    handler = asyncio.run(scenario())
    assert len(handler._last_sent) <= 10 and handler.dropped > 0
