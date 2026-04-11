import logging
import os

from dotenv import load_dotenv
from sentry_sdk import init as sentry_init
from sentry_sdk.integrations.logging import LoggingIntegration

from monitoring.telegram_errors import is_transient_telegram_error, is_transient_telegram_error_data

load_dotenv()


def _event_contains_transient_telegram_error(event: dict, hint: dict) -> bool:
    exc_info = hint.get("exc_info")
    if exc_info and is_transient_telegram_error(exc_info[1]):
        return True

    logger_name = event.get("logger", "")
    values = event.get("exception", {}).get("values", [])
    for value in values:
        if is_transient_telegram_error_data(
            type_name=value.get("type"),
            message=value.get("value"),
            logger_name=logger_name,
        ):
            return True
    return False


def _before_send(event: dict, hint: dict) -> dict | None:
    if _event_contains_transient_telegram_error(event, hint):
        return None
    return event


def init_sentry(service_name: str) -> bool:
    """Initialize Sentry only when DSN is configured."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    logging_integration = LoggingIntegration(
        level=logging.INFO,
        event_level=logging.ERROR,
    )
    sentry_init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        before_send=_before_send,
        integrations=[logging_integration],
    )
    logging.getLogger(__name__).info("Sentry initialized for %s", service_name)
    return True
