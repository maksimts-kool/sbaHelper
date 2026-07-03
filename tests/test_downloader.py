import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
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

from downloader.config import (
    MAX_SHORT_DURATION_SEC,
    LinkCheck,
    LinkCheckResult,
    exceeds_short_limit,
    extract_supported_url,
    is_non_video_url,
    is_vertical_video,
    print_results,
    result_exit_code,
    run_check,
)
from downloader.download import (
    DownloadError,
    UnsupportedContentError,
    VideoTooLongError,
    _file_has_audio_stream,
    fetch_info,
    get_format_selector,
    normalize_video_info,
    parse_compact_count,
    pick_video_dimensions,
)
from downloader.formatting import build_download_progress_line, build_video_caption, format_count
from shared import print_results as shared_print_results


class DownloaderStartupChecksTest(unittest.TestCase):
    def test_unsupported_content_warning_does_not_block_startup(self) -> None:
        with patch(
            "downloader.download.fetch_info",
            side_effect=UnsupportedContentError("Photo posts are not supported."),
        ):
            result = run_check(LinkCheck("facebook", "https://www.facebook.com/reel/123"))

        self.assertFalse(result.ok)
        self.assertFalse(result.blocks_startup)
        self.assertEqual(result_exit_code([result]), 0)

    def test_tiktok_forbidden_warning_does_not_block_startup(self) -> None:
        with patch(
            "downloader.download.fetch_info",
            side_effect=DownloadError("HTTP Error 403: Forbidden"),
        ):
            result = run_check(LinkCheck("tiktok", "https://www.tiktok.com/@user/video/123"))

        self.assertFalse(result.ok)
        self.assertFalse(result.blocks_startup)
        self.assertEqual(result_exit_code([result]), 0)

    def test_missing_url_is_skipped(self) -> None:
        # Пустой CHECK_* URL теперь не блокирует запуск, а тихо пропускается.
        result = run_check(LinkCheck("youtube", ""))

        self.assertTrue(result.ok)
        self.assertFalse(result.blocks_startup)
        self.assertEqual(result_exit_code([result]), 0)

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
            "downloader.download.subprocess.run",
            return_value=types.SimpleNamespace(
                stdout='{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}'
            ),
        ):
            self.assertTrue(_file_has_audio_stream("/tmp/video.mp4"))

    def test_audio_stream_probe_detects_missing_audio(self) -> None:
        with patch(
            "downloader.download.subprocess.run",
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
        self.assertEqual(extract_supported_url(text), "https://www.tiktok.com/@user/video/123")

    def test_ignores_unrelated_domain(self) -> None:
        self.assertIsNone(extract_supported_url("https://example.com/video/123"))


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
            "https://www.facebook.com/profile.php?id=123",
            "https://www.facebook.com/groups/somegroup",
        ]
        for url in non_video:
            with self.subTest(url=url):
                self.assertTrue(is_non_video_url(url))

    def test_actual_video_urls_are_not_flagged(self) -> None:
        videos = [
            "https://www.tiktok.com/@ivanova197/video/7234567890",
            "https://vm.tiktok.com/ZSabc123/",
            "https://youtube.com/shorts/abcdEFGhij",
            "https://www.youtube.com/watch?v=abcdEFGhij",
            "https://youtu.be/abcdEFGhij",
            "https://www.facebook.com/reel/123",
            "https://www.facebook.com/user/videos/123",
        ]
        for url in videos:
            with self.subTest(url=url):
                self.assertFalse(is_non_video_url(url))


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


class FetchInfoVerificationTest(unittest.TestCase):
    """fetch_info должен принимать только короткие вертикальные видео."""

    @staticmethod
    def _meta(**overrides) -> dict:
        meta = {
            "title": "Clip",
            "uploader": "Author",
            "duration": 30,
            "width": 720,
            "height": 1280,
            "formats": [{"vcodec": "h264", "width": 720, "height": 1280}],
        }
        meta.update(overrides)
        return meta

    def test_horizontal_video_is_rejected_before_duration_check(self) -> None:
        # Длинное горизонтальное видео должно отклоняться как «не вертикальное»,
        # а не как «слишком длинное».
        meta = self._meta(duration=1128, width=1920, height=1080)
        with patch("downloader.download._extract_with_retries", return_value=meta):
            with self.assertRaises(UnsupportedContentError):
                fetch_info("https://youtu.be/abc123")

    def test_long_vertical_video_is_rejected_as_too_long(self) -> None:
        meta = self._meta(duration=1128, width=720, height=1280)
        with patch("downloader.download._extract_with_retries", return_value=meta):
            with self.assertRaises(VideoTooLongError):
                fetch_info("https://youtu.be/abc123")

    def test_short_vertical_video_is_accepted(self) -> None:
        meta = self._meta(duration=30, width=720, height=1280)
        with patch("downloader.download._extract_with_retries", return_value=meta):
            info = fetch_info("https://youtu.be/abc123")

        self.assertEqual(info.width, 720)
        self.assertEqual(info.height, 1280)

    def test_playlist_or_profile_is_rejected(self) -> None:
        meta = {"_type": "playlist", "entries": [{"id": "a"}, {"id": "b"}]}
        with patch("downloader.download._extract_with_retries", return_value=meta):
            with self.assertRaises(UnsupportedContentError):
                fetch_info("https://www.tiktok.com/@ivanova197/video/123")


class DownloaderHelpersTest(unittest.TestCase):
    def test_downloader_count_and_progress_formatting(self) -> None:
        self.assertEqual(format_count(999), "999")
        self.assertEqual(format_count(1_200), "1.2K")
        self.assertEqual(format_count(2_000_000), "2M")
        self.assertIn("100%", build_download_progress_line(125))

    def test_downloader_caption_prevents_auto_links(self) -> None:
        info = SimpleNamespace(
            title="Watch https://example.com #tag",
            uploader="@creator",
            duration=65,
            view_count=1_500,
            like_count=None,
        )

        caption = build_video_caption(info)

        self.assertIn("1:⁠05", caption)
        self.assertIn("h⁠ttps://example.com", caption)
        self.assertIn("#⁠tag", caption)
        self.assertIn("@⁠creator", caption)
        self.assertIn("1.5K просмотров", caption)

    def test_metadata_normalizes_facebook_title_stats(self) -> None:
        info = normalize_video_info(
            "https://www.facebook.com/reel/123",
            {
                "title": "1.2K views 45 reactions | Real title | Author Name",
                "uploader": "",
                "duration": 10,
            },
            10,
        )

        self.assertEqual(info.title, "Real title")
        self.assertEqual(info.uploader, "Author Name")
        self.assertEqual(info.view_count, 1200)
        self.assertEqual(info.like_count, 45)

    def test_metadata_parses_compact_counts(self) -> None:
        self.assertEqual(parse_compact_count("1.5K"), 1500)
        self.assertEqual(parse_compact_count("2 M"), 2_000_000)
        self.assertIsNone(parse_compact_count("many"))

    def test_shared_startup_printer_supports_warnings(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            shared_print_results(
                [
                    SimpleNamespace(service="ok", ok=True, message="fine", blocks_startup=False),
                    SimpleNamespace(service="warn", ok=False, message="soft", blocks_startup=False),
                    SimpleNamespace(name="fail", ok=False, message="hard", blocks_startup=True),
                ]
            )

        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "OK ok: fine",
                "WARN warn: soft",
                "FAIL fail: hard",
            ],
        )


if __name__ == "__main__":
    unittest.main()
