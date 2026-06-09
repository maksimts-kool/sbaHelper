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

from downloader.core import DownloadError, UnsupportedContentError, _file_has_audio_stream
from downloader.metadata import pick_video_dimensions
from downloader.service import (
    MAX_SHORT_DURATION_SEC,
    LinkCheck,
    LinkCheckResult,
    exceeds_short_limit,
    extract_supported_url,
    is_vertical_video,
    print_results,
    result_exit_code,
    run_check,
)
from downloader.ytdlp_options import get_format_selector


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

    def test_tiktok_download_selector_prefers_audio_bearing_formats(self) -> None:
        selector = get_format_selector("https://www.tiktok.com/@user/video/123")
        choices = selector.split("/")

        self.assertIn("[vcodec=h264]", choices[0])
        self.assertIn("[filesize<", choices[0])
        self.assertIn("[acodec!=none]", selector)
        self.assertIn("+bestaudio", selector)
        self.assertLess(selector.index("[vcodec=h264]"), selector.index("+bestaudio"))

    def test_audio_stream_probe_detects_audio(self) -> None:
        with patch(
            "downloader.core.subprocess.run",
            return_value=types.SimpleNamespace(
                stdout='{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}'
            ),
        ):
            self.assertTrue(_file_has_audio_stream("/tmp/video.mp4"))

    def test_audio_stream_probe_detects_missing_audio(self) -> None:
        with patch(
            "downloader.core.subprocess.run",
            return_value=types.SimpleNamespace(stdout='{"streams":[{"codec_type":"video"}]}'),
        ):
            self.assertFalse(_file_has_audio_stream("/tmp/video.mp4"))


class SupportedUrlDetectionTest(unittest.TestCase):
    def test_accepts_any_main_domain_link(self) -> None:
        urls = [
            "https://www.youtube.com/watch?v=abcdEFGhij",
            "https://youtube.com/shorts/abcdEFGhij",
            "https://m.youtube.com/watch?v=abcdEFGhij",
            "https://youtu.be/abcdEFGhij",
            "https://www.tiktok.com/@user/video/123",
            "https://vm.tiktok.com/ZSabc123/",
            "https://www.facebook.com/reel/123",
            "https://web.facebook.com/some.user/posts/123",
            "https://fb.watch/abc123/",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(extract_supported_url(url), url)

    def test_extracts_link_from_surrounding_text(self) -> None:
        text = "смотри это https://www.tiktok.com/@user/video/123 круто!"
        self.assertEqual(
            extract_supported_url(text), "https://www.tiktok.com/@user/video/123"
        )

    def test_ignores_unrelated_domain(self) -> None:
        self.assertIsNone(extract_supported_url("https://example.com/video/123"))


class ShortVideoLimitTest(unittest.TestCase):
    def test_short_or_unknown_duration_is_allowed(self) -> None:
        self.assertFalse(exceeds_short_limit(0))
        self.assertFalse(exceeds_short_limit(60))
        self.assertFalse(exceeds_short_limit(MAX_SHORT_DURATION_SEC))

    def test_long_video_is_rejected(self) -> None:
        self.assertTrue(exceeds_short_limit(MAX_SHORT_DURATION_SEC + 1))


class VerticalVideoCheckTest(unittest.TestCase):
    def test_vertical_video_is_accepted(self) -> None:
        self.assertTrue(is_vertical_video(1080, 1920))

    def test_horizontal_and_square_videos_are_rejected(self) -> None:
        self.assertFalse(is_vertical_video(1920, 1080))
        self.assertFalse(is_vertical_video(1080, 1080))

    def test_unknown_dimensions_are_allowed(self) -> None:
        self.assertTrue(is_vertical_video(None, None))
        self.assertTrue(is_vertical_video(0, 0))

    def test_dimensions_fall_back_to_formats(self) -> None:
        meta = {"formats": [{"width": 720, "height": 1280}]}
        self.assertEqual(pick_video_dimensions(meta), (720, 1280))

    def test_top_level_dimensions_take_priority(self) -> None:
        meta = {"width": 1080, "height": 1920, "formats": [{"width": 1, "height": 1}]}
        self.assertEqual(pick_video_dimensions(meta), (1080, 1920))


if __name__ == "__main__":
    unittest.main()
