from __future__ import annotations

import logging


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
