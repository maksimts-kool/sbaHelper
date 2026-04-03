def is_transient_telegram_error(error: BaseException | None) -> bool:
    """Return True for Telegram transport failures that are expected to self-heal."""
    if error is None:
        return False

    error_type = type(error)
    return (
        error_type.__name__ == "NetworkError"
        and error_type.__module__.startswith("telegram")
        and "Bad Gateway" in str(error)
    )
