"""Настройки, разбор ссылок и стартовые проверки — `downloader/config.py`."""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import time
from types import SimpleNamespace
from unittest.mock import patch

from downloader.config import (
    MAX_SHORT_DURATION_SEC,
    LinkCheck,
    LinkCheckResult,
    configured_checks,
    detect_platform,
    env_clock_time,
    env_flag,
    env_int,
    exceeds_short_limit,
    extract_supported_url,
    is_non_video_url,
    is_nonblocking_check_error,
    is_transient_network_error,
    is_vertical_video,
    print_results,
    result_exit_code,
    run_check,
)
from shared import print_results as shared_print_results


class EnvHelpersTest(unittest.TestCase):
    """Опечатка в переменной окружения не должна ронять бота при старте."""

    def test_flag_defaults_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(env_flag("MISSING", default=True))
            self.assertFalse(env_flag("MISSING", default=False))

    def test_flag_reads_falsey_words(self) -> None:
        for raw in ("0", "false", "FALSE", "no", "off", " Off "):
            with self.subTest(raw=raw), patch.dict(os.environ, {"FLAG": raw}):
                self.assertFalse(env_flag("FLAG", default=True))

    def test_flag_treats_anything_else_as_true(self) -> None:
        for raw in ("1", "true", "yes", "", "whatever"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"FLAG": raw}):
                self.assertTrue(env_flag("FLAG", default=False))

    def test_int_falls_back_on_garbage(self) -> None:
        with patch.dict(os.environ, {"NUM": "12"}):
            self.assertEqual(env_int("NUM", 5), 12)
        with patch.dict(os.environ, {"NUM": "later"}):
            self.assertEqual(env_int("NUM", 5), 5)
        with patch.dict(os.environ, {"NUM": "  "}):
            self.assertEqual(env_int("NUM", 5), 5)

    def test_clock_time_accepts_hh_mm(self) -> None:
        with patch.dict(os.environ, {"AT": "9:05"}):
            self.assertEqual(env_clock_time("AT", time(20)), time(9, 5))
        with patch.dict(os.environ, {"AT": "23:59"}):
            self.assertEqual(env_clock_time("AT", time(20)), time(23, 59))

    def test_clock_time_falls_back_on_invalid_values(self) -> None:
        for raw in ("", "20", "20:00:00", "25:00", "20:60", "восемь"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"AT": raw}):
                self.assertEqual(env_clock_time("AT", time(20)), time(20))


class SupportedUrlTest(unittest.TestCase):
    def test_accepts_any_subdomain_of_a_supported_site(self) -> None:
        urls = [
            "https://www.youtube.com/watch?v=abcdEFGhij",
            "https://youtube.com/shorts/abcdEFGhij",
            "https://m.youtube.com/watch?v=abcdEFGhij",
            "https://youtu.be/abcdEFGhij",
            "https://www.tiktok.com/@user/video/123",
            "https://vm.tiktok.com/ZSabc123/",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(extract_supported_url(url), url)

    def test_extracts_a_link_from_surrounding_text(self) -> None:
        text = "смотри это https://www.tiktok.com/@user/video/123 круто!"
        self.assertEqual(extract_supported_url(text), "https://www.tiktok.com/@user/video/123")

    def test_strips_trailing_punctuation(self) -> None:
        self.assertEqual(
            extract_supported_url("вот (https://youtu.be/abc123)."),
            "https://youtu.be/abc123",
        )

    def test_ignores_unsupported_sites(self) -> None:
        for url in (
            "https://example.com/video/123",
            "https://www.facebook.com/reel/123",
            "https://fb.watch/abc123/",
            "https://vimeo.com/123456",
        ):
            with self.subTest(url=url):
                self.assertIsNone(extract_supported_url(url))


class NonVideoUrlTest(unittest.TestCase):
    def test_profiles_channels_and_listings_are_flagged(self) -> None:
        non_video = [
            "https://www.tiktok.com/@ivanova197",
            "https://www.tiktok.com/@ivanova197/",
            "https://www.tiktok.com/tag/cats",
            "https://m.tiktok.com/@user",
            "https://www.youtube.com/@MrBeast",
            "https://www.youtube.com/channel/UCabc123",
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/shorts",
        ]
        for url in non_video:
            with self.subTest(url=url):
                self.assertTrue(is_non_video_url(url))

    def test_links_to_a_single_video_are_not_flagged(self) -> None:
        videos = [
            "https://www.tiktok.com/@ivanova197/video/7234567890",
            "https://vm.tiktok.com/ZSabc123/",
            "https://youtube.com/shorts/abcdEFGhij",
            "https://www.youtube.com/watch?v=abcdEFGhij",
            "https://youtu.be/abcdEFGhij",
        ]
        for url in videos:
            with self.subTest(url=url):
                self.assertFalse(is_non_video_url(url))


class PlatformDetectionTest(unittest.TestCase):
    def test_detects_the_supported_platforms(self) -> None:
        cases = {
            "https://www.tiktok.com/@user/video/123": "tiktok",
            "https://vm.tiktok.com/ZSabc123/": "tiktok",
            "https://youtube.com/shorts/abc": "youtube",
            "https://youtu.be/abc": "youtube",
            "https://example.com/clip": "other",
            "https://www.facebook.com/reel/123": "other",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_platform(url), expected)


class VideoLimitsTest(unittest.TestCase):
    def test_short_or_unknown_duration_is_allowed(self) -> None:
        self.assertFalse(exceeds_short_limit(0))
        self.assertFalse(exceeds_short_limit(60))
        self.assertFalse(exceeds_short_limit(MAX_SHORT_DURATION_SEC))

    def test_long_video_is_rejected(self) -> None:
        self.assertTrue(exceeds_short_limit(MAX_SHORT_DURATION_SEC + 1))

    def test_only_portrait_video_is_accepted(self) -> None:
        self.assertTrue(is_vertical_video(1080, 1920))
        self.assertFalse(is_vertical_video(1920, 1080))
        self.assertFalse(is_vertical_video(1080, 1080))

    def test_unknown_dimensions_are_allowed(self) -> None:
        self.assertTrue(is_vertical_video(None, None))
        self.assertTrue(is_vertical_video(0, 0))


class TransientErrorTest(unittest.TestCase):
    def test_network_wording_is_treated_as_transient(self) -> None:
        for error in (
            OSError("Connection reset by peer"),
            RuntimeError("The read operation timed out"),
            ValueError("All connection attempts failed"),
        ):
            with self.subTest(error=error):
                self.assertTrue(is_transient_network_error(error))

    def test_real_failures_are_not_transient(self) -> None:
        self.assertFalse(is_transient_network_error(ValueError("Video unavailable")))

    def test_transient_errors_never_block_startup(self) -> None:
        check = LinkCheck("youtube", "https://youtu.be/abc")
        self.assertTrue(is_nonblocking_check_error(check, OSError("connection reset")))

    def test_tiktok_403_is_tolerated_but_youtube_403_is_not(self) -> None:
        forbidden = RuntimeError("HTTP Error 403: Forbidden")
        self.assertTrue(is_nonblocking_check_error(LinkCheck("tiktok", "u"), forbidden))
        self.assertFalse(is_nonblocking_check_error(LinkCheck("youtube", "u"), forbidden))


class StartupCheckTest(unittest.TestCase):
    def test_only_youtube_and_tiktok_are_checked(self) -> None:
        self.assertEqual([check.service for check in configured_checks()], ["youtube", "tiktok"])

    def test_unconfigured_check_is_skipped(self) -> None:
        result = run_check(LinkCheck("youtube", ""))

        self.assertTrue(result.ok)
        self.assertFalse(result.blocks_startup)
        self.assertEqual(result_exit_code([result]), 0)

    def test_unsupported_url_format_blocks_startup(self) -> None:
        result = run_check(LinkCheck("youtube", "https://example.com/clip"))

        self.assertFalse(result.ok)
        self.assertTrue(result.blocks_startup)
        self.assertEqual(result_exit_code([result]), 1)

    def test_unsupported_content_is_a_warning(self) -> None:
        from downloader.download import UnsupportedContentError

        with patch(
            "downloader.download.fetch_info",
            side_effect=UnsupportedContentError("Здесь только фото."),
        ):
            result = run_check(LinkCheck("tiktok", "https://www.tiktok.com/@user/video/123"))

        self.assertFalse(result.ok)
        self.assertFalse(result.blocks_startup)
        self.assertEqual(result_exit_code([result]), 0)

    def test_tiktok_forbidden_is_a_warning(self) -> None:
        from downloader.download import DownloadError

        with patch(
            "downloader.download.fetch_info",
            side_effect=DownloadError("HTTP Error 403: Forbidden"),
        ):
            result = run_check(LinkCheck("tiktok", "https://www.tiktok.com/@user/video/123"))

        self.assertFalse(result.ok)
        self.assertFalse(result.blocks_startup)

    def test_hard_download_error_blocks_startup(self) -> None:
        from downloader.download import DownloadError

        with patch(
            "downloader.download.fetch_info",
            side_effect=DownloadError("Video unavailable"),
        ):
            result = run_check(LinkCheck("youtube", "https://youtu.be/abc123"))

        self.assertFalse(result.ok)
        self.assertTrue(result.blocks_startup)
        self.assertEqual(result_exit_code([result]), 1)

    def test_successful_check_reports_the_video(self) -> None:
        info = SimpleNamespace(title="Clip", uploader="Author", duration=30)
        with patch("downloader.download.fetch_info", return_value=info):
            result = run_check(LinkCheck("youtube", "https://youtu.be/abc123"))

        self.assertTrue(result.ok)
        self.assertEqual(result.message, "Clip by Author (30s)")


class ResultPrintingTest(unittest.TestCase):
    def _capture(self, printer, results) -> list[str]:
        output = io.StringIO()
        with redirect_stdout(output):
            printer(results)
        return output.getvalue().splitlines()

    def test_nonblocking_failures_print_as_warnings(self) -> None:
        lines = self._capture(
            print_results,
            [
                LinkCheckResult("youtube", True, "ok", blocks_startup=False),
                LinkCheckResult("tiktok", False, "photo", blocks_startup=False),
                LinkCheckResult("youtube", False, "missing url"),
            ],
        )

        self.assertEqual(
            lines, ["OK youtube: ok", "WARN tiktok: photo", "FAIL youtube: missing url"]
        )

    def test_shared_printer_accepts_service_or_name(self) -> None:
        lines = self._capture(
            shared_print_results,
            [
                SimpleNamespace(service="ok", ok=True, message="fine", blocks_startup=False),
                SimpleNamespace(name="fail", ok=False, message="hard", blocks_startup=True),
            ],
        )

        self.assertEqual(lines, ["OK ok: fine", "FAIL fail: hard"])


if __name__ == "__main__":
    unittest.main()
