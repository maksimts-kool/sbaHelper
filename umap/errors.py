from __future__ import annotations

import logging
from typing import Any

import httpx
import sentry_sdk
from aiogram.exceptions import TelegramNetworkError

from umap.settings import env


logger = logging.getLogger(__name__)
_sentry_initialized = False
_TRANSIENT_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_TRANSIENT_ERROR_TEXT = (
    "all connection attempts failed",
    "bad gateway",
    "cannot connect to host",
    "connection has been closed",
    "connection reset",
    "readtimeout",
    "server disconnected",
    "temporarily unavailable",
    "timed out",
    "timeout",
)


def is_transient_network_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))

        if isinstance(current, httpx.HTTPStatusError):
            return current.response.status_code in _TRANSIENT_HTTP_STATUS_CODES
        if isinstance(current, (httpx.TimeoutException, httpx.TransportError, TelegramNetworkError)):
            return True

        text = str(current).lower()
        if any(marker in text for marker in _TRANSIENT_ERROR_TEXT):
            return True

        current = current.__cause__ or current.__context__

    return False


def _sentry_before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    exc_info = hint.get("exc_info")
    if exc_info and len(exc_info) >= 2 and isinstance(exc_info[1], BaseException):
        if is_transient_network_error(exc_info[1]):
            return None

    logentry = event.get("logentry")
    logentry_message = logentry.get("message") if isinstance(logentry, dict) else logentry
    message = " ".join(
        str(part)
        for part in (
            event.get("message"),
            logentry_message,
        )
        if part
    ).lower()
    if message and any(marker in message for marker in _TRANSIENT_ERROR_TEXT):
        return None

    return event


def init_error_tracking(service_name: str) -> bool:
    global _sentry_initialized

    if _sentry_initialized:
        sentry_sdk.set_tag("service", service_name)
        return True

    dsn = env("SENTRY_DSN")
    if not dsn:
        logger.debug("Sentry is not configured.")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=env("SENTRY_ENVIRONMENT") or None,
        release=env("SENTRY_RELEASE") or None,
        traces_sample_rate=0.0,
        before_send=_sentry_before_send,
    )
    sentry_sdk.set_tag("service", service_name)
    _sentry_initialized = True
    logger.info("Sentry error tracking enabled for %s.", service_name)
    return True


def capture_exception(error: BaseException) -> None:
    if _sentry_initialized and not is_transient_network_error(error):
        sentry_sdk.capture_exception(error)


def flush_error_tracking(timeout: float = 2.0) -> None:
    if _sentry_initialized:
        sentry_sdk.flush(timeout=timeout)
