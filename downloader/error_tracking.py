"""
Optional Sentry error tracking.
"""
import logging

import sentry_sdk

from downloader.config import SENTRY_DSN, SENTRY_ENVIRONMENT, SENTRY_RELEASE

logger = logging.getLogger(__name__)
_initialized = False


def init_error_tracking(service_name: str) -> bool:
    """Enable Sentry when SENTRY_DSN is configured."""
    global _initialized
    if _initialized:
        sentry_sdk.set_tag("service", service_name)
        return True

    if not SENTRY_DSN:
        logger.debug("Sentry is not configured.")
        return False

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT or None,
        release=SENTRY_RELEASE or None,
        traces_sample_rate=0.0,
    )
    sentry_sdk.set_tag("service", service_name)
    _initialized = True
    logger.info("Sentry error tracking enabled for %s.", service_name)
    return True


def capture_exception(error: BaseException) -> None:
    if _initialized:
        sentry_sdk.capture_exception(error)


def flush_error_tracking(timeout: float = 2.0) -> None:
    if _initialized:
        sentry_sdk.flush(timeout=timeout)
