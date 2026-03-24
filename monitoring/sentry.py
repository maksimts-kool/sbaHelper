import logging
import os

from dotenv import load_dotenv
from sentry_sdk import init as sentry_init
from sentry_sdk.integrations.logging import LoggingIntegration

load_dotenv()


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
        integrations=[logging_integration],
    )
    logging.getLogger(__name__).info("Sentry initialized for %s", service_name)
    return True
