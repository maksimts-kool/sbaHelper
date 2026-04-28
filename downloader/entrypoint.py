"""
Container entrypoint.
Runs link checks before starting the Telegram bot.
"""
import logging

from downloader.bot import main as run_bot
from downloader.check_links import print_results, result_exit_code, run_checks
from downloader.config import STARTUP_CHECKS_REQUIRED
from downloader.error_tracking import flush_error_tracking, init_error_tracking

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    init_error_tracking("downloader-startup")

    logger.info("Running startup link checks.")
    results = run_checks()
    print_results(results)
    check_exit_code = result_exit_code(results)
    flush_error_tracking()

    if check_exit_code != 0:
        if STARTUP_CHECKS_REQUIRED:
            logger.error("Startup link checks failed with exit code %s.", check_exit_code)
            return check_exit_code
        logger.warning(
            "Startup link checks failed with exit code %s. Continuing because STARTUP_CHECKS_REQUIRED=0.",
            check_exit_code,
        )

    return run_bot()


if __name__ == "__main__":
    raise SystemExit(main())
