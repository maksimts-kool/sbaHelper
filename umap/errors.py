from __future__ import annotations

import logging

import httpx
from aiogram.exceptions import TelegramNetworkError

from sbahelper.errors import DEFAULT_TRANSIENT_ERROR_TEXT, SentryTracker, is_transient_error
from umap.settings import env


logger = logging.getLogger(__name__)
_TRANSIENT_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
_TRANSIENT_ERROR_TEXT = (
    "bad gateway",
) + DEFAULT_TRANSIENT_ERROR_TEXT


def _transient_status_code(error: BaseException) -> int | None:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    return None


def is_transient_network_error(error: BaseException) -> bool:
    return is_transient_error(
        error,
        exception_types=(httpx.TimeoutException, httpx.TransportError, TelegramNetworkError),
        text_markers=_TRANSIENT_ERROR_TEXT,
        status_code_getter=_transient_status_code,
        transient_status_codes=_TRANSIENT_HTTP_STATUS_CODES,
    )


_sentry_tracker = SentryTracker(
    dsn_getter=lambda: env("SENTRY_DSN"),
    environment_getter=lambda: env("SENTRY_ENVIRONMENT"),
    release_getter=lambda: env("SENTRY_RELEASE"),
    is_transient=is_transient_network_error,
    text_markers=_TRANSIENT_ERROR_TEXT,
)


def init_error_tracking(service_name: str) -> bool:
    return _sentry_tracker.init(service_name)


def capture_exception(error: BaseException) -> None:
    _sentry_tracker.capture_exception(error)


def flush_error_tracking(timeout: float = 2.0) -> None:
    _sentry_tracker.flush(timeout=timeout)
