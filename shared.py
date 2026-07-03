"""
Shared helpers used by both bots: logging setup, transient-error detection,
Sentry tracking, and startup-check result printing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol, cast

import sentry_sdk

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def configure_logging(
    log_level: str = "INFO",
    *,
    quiet_loggers: tuple[str, ...] = (),
) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    for logger_name in quiet_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Transient errors + Sentry
# --------------------------------------------------------------------------- #

DEFAULT_TRANSIENT_ERROR_TEXT = (
    "all connection attempts failed",
    "cannot connect to host",
    "connection has been closed",
    "connection reset",
    "readtimeout",
    "server disconnected",
    "temporarily unavailable",
    "timed out",
    "timeout",
)


def iter_exception_chain(error: BaseException):
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_transient_error(
    error: BaseException,
    *,
    exception_types: tuple[type[BaseException], ...] = (),
    text_markers: tuple[str, ...] = DEFAULT_TRANSIENT_ERROR_TEXT,
    status_code_getter: Callable[[BaseException], int | None] | None = None,
    transient_status_codes: set[int] | frozenset[int] = frozenset(),
) -> bool:
    lowered_markers = tuple(marker.lower() for marker in text_markers)
    for current in iter_exception_chain(error):
        if exception_types and isinstance(current, exception_types):
            return True

        if status_code_getter is not None:
            status_code = status_code_getter(current)
            if status_code is not None and status_code in transient_status_codes:
                return True

        text = str(current).lower()
        if any(marker in text for marker in lowered_markers):
            return True

    return False


class SentryTracker:
    def __init__(
        self,
        *,
        dsn_getter: Callable[[], str],
        environment_getter: Callable[[], str],
        release_getter: Callable[[], str],
        is_transient: Callable[[BaseException], bool],
        text_markers: tuple[str, ...] = DEFAULT_TRANSIENT_ERROR_TEXT,
    ) -> None:
        self._dsn_getter = dsn_getter
        self._environment_getter = environment_getter
        self._release_getter = release_getter
        self._is_transient = is_transient
        self._text_markers = tuple(marker.lower() for marker in text_markers)
        self._initialized = False

    def before_send(self, event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
        exc_info = hint.get("exc_info")
        if (
            exc_info
            and len(exc_info) >= 2
            and isinstance(exc_info[1], BaseException)
            and self._is_transient(exc_info[1])
        ):
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
        if message and any(marker in message for marker in self._text_markers):
            return None

        return event

    def init(self, service_name: str) -> bool:
        if self._initialized:
            sentry_sdk.set_tag("service", service_name)
            return True

        dsn = self._dsn_getter().strip()
        if not dsn:
            logger.debug("Sentry is not configured.")
            return False

        sentry_sdk.init(
            dsn=dsn,
            environment=self._environment_getter().strip() or None,
            release=self._release_getter().strip() or None,
            traces_sample_rate=0.0,
            before_send=cast(Any, self.before_send),
        )
        sentry_sdk.set_tag("service", service_name)
        self._initialized = True
        logger.info("Sentry error tracking enabled for %s.", service_name)
        return True

    def capture_exception(self, error: BaseException) -> None:
        if self._initialized and not self._is_transient(error):
            sentry_sdk.capture_exception(error)

    def flush(self, timeout: float = 2.0) -> None:
        if self._initialized:
            sentry_sdk.flush(timeout=timeout)


# --------------------------------------------------------------------------- #
# Startup-check result printing
# --------------------------------------------------------------------------- #


class StartupCheckResult(Protocol):
    @property
    def ok(self) -> bool: ...

    @property
    def message(self) -> str: ...

    @property
    def blocks_startup(self) -> bool: ...


def result_label(result: StartupCheckResult) -> str:
    if result.ok:
        return "OK"
    if result.blocks_startup:
        return "FAIL"
    return "WARN"


def result_name(result: StartupCheckResult) -> str:
    return str(getattr(result, "service", getattr(result, "name", "")))


def print_results(results: Sequence[StartupCheckResult]) -> None:
    for result in results:
        print(f"{result_label(result)} {result_name(result)}: {result.message}")
