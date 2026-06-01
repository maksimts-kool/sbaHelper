import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


def _install_runtime_stubs() -> None:
    sentry_sdk = types.ModuleType("sentry_sdk")
    sentry_sdk.init = lambda **kwargs: None
    sentry_sdk.set_tag = lambda *args, **kwargs: None
    sentry_sdk.capture_exception = lambda *args, **kwargs: None
    sentry_sdk.flush = lambda *args, **kwargs: None
    sys.modules.setdefault("sentry_sdk", sentry_sdk)

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules.setdefault("dotenv", dotenv)

    telegram = types.ModuleType("telegram")
    telegram_error = types.ModuleType("telegram.error")

    class NetworkError(Exception):
        pass

    telegram_error.NetworkError = NetworkError
    sys.modules.setdefault("telegram", telegram)
    sys.modules.setdefault("telegram.error", telegram_error)

    yt_dlp = types.ModuleType("yt_dlp")
    yt_dlp_utils = types.ModuleType("yt_dlp.utils")

    class YtdlpDownloadError(Exception):
        pass

    yt_dlp_utils.DownloadError = YtdlpDownloadError
    yt_dlp.utils = yt_dlp_utils
    yt_dlp.YoutubeDL = object
    sys.modules.setdefault("yt_dlp", yt_dlp)
    sys.modules.setdefault("yt_dlp.utils", yt_dlp_utils)


_install_runtime_stubs()

from downloader.core import DownloadError, UnsupportedContentError
from downloader.service import LinkCheck, LinkCheckResult, print_results, result_exit_code, run_check


class DownloaderStartupChecksTest(unittest.TestCase):
    def test_unsupported_content_warning_does_not_block_startup(self) -> None:
        with patch(
            "downloader.core.fetch_info",
            side_effect=UnsupportedContentError("Photo posts are not supported."),
        ):
            result = run_check(LinkCheck("facebook", "https://www.facebook.com/reel/123"))

        self.assertFalse(result.ok)
        self.assertFalse(result.blocks_startup)
        self.assertEqual(result_exit_code([result]), 0)

    def test_tiktok_forbidden_warning_does_not_block_startup(self) -> None:
        with patch(
            "downloader.core.fetch_info",
            side_effect=DownloadError("HTTP Error 403: Forbidden"),
        ):
            result = run_check(LinkCheck("tiktok", "https://www.tiktok.com/@user/video/123"))

        self.assertFalse(result.ok)
        self.assertFalse(result.blocks_startup)
        self.assertEqual(result_exit_code([result]), 0)

    def test_missing_url_still_blocks_startup(self) -> None:
        result = run_check(LinkCheck("youtube", ""))

        self.assertFalse(result.ok)
        self.assertTrue(result.blocks_startup)
        self.assertEqual(result_exit_code([result]), 2)

    def test_print_results_labels_nonblocking_failures_as_warnings(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            print_results(
                [
                    LinkCheckResult("youtube", True, "ok", blocks_startup=False),
                    LinkCheckResult("facebook", False, "photo", blocks_startup=False),
                    LinkCheckResult("youtube", False, "missing url"),
                ]
            )

        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "OK youtube: ok",
                "WARN facebook: photo",
                "FAIL youtube: missing url",
            ],
        )


if __name__ == "__main__":
    unittest.main()
