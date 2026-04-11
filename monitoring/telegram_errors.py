_STALE_CALLBACK_QUERY_MARKERS = (
    "query is too old",
    "query id is invalid",
    "response timeout expired",
)


def is_transient_telegram_error(error: BaseException | None) -> bool:
    """Return True for Telegram/API failures that are expected to self-heal."""
    if error is None:
        return False

    error_type = type(error)
    return is_transient_telegram_error_data(
        type_name=error_type.__name__,
        message=str(error),
        module=error_type.__module__,
    )


def is_transient_telegram_error_data(
    type_name: str | None,
    message: str | None,
    module: str | None = None,
    logger_name: str | None = None,
) -> bool:
    """Classify Telegram errors using primitive event data.

    This is used both for live exception objects and for Sentry event payloads.
    """
    if not type_name:
        return False

    is_telegram_source = bool(
        (module and module.startswith("telegram"))
        or (logger_name and logger_name.startswith("telegram"))
    )
    if not is_telegram_source:
        return False

    normalized_message = (message or "").lower()

    if type_name in {"NetworkError", "TimedOut"}:
        return True

    return (
        type_name == "BadRequest"
        and any(marker in normalized_message for marker in _STALE_CALLBACK_QUERY_MARKERS)
    )
