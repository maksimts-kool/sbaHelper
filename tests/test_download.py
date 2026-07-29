"""Движок загрузки — `downloader/download.py`.

Настоящий yt-dlp и сеть не используются: `_extract_with_retries` подменяется,
а «скачанный» файл создаётся во временной папке.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yt_dlp

from downloader.download import (
    DownloadError,
    FileTooLargeError,
    UnsupportedContentError,
    VideoTooLongError,
    _extract_with_retries,
    _file_has_audio_stream,
    _rewrite_download_error,
    build_ydl_opts,
    cleanup,
    download_video,
    fetch_info,
    get_format_selector,
    normalize_video_info,
    parse_compact_count,
    pick_video_dimensions,
)

TIKTOK_URL = "https://www.tiktok.com/@user/video/123"
YOUTUBE_URL = "https://youtu.be/abc123"


def video_meta(**overrides) -> dict:
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


class YdlOptionsTest(unittest.TestCase):
    def test_metadata_mode_does_not_expand_playlists(self) -> None:
        opts = build_ydl_opts(YOUTUBE_URL, download=False)

        self.assertTrue(opts["skip_download"])
        self.assertTrue(opts["noplaylist"])
        self.assertEqual(opts["extract_flat"], "in_playlist")

    def test_download_mode_needs_an_output_template(self) -> None:
        with self.assertRaises(ValueError):
            build_ydl_opts(YOUTUBE_URL, download=True)

    def test_download_mode_sets_output_and_format(self) -> None:
        opts = build_ydl_opts(YOUTUBE_URL, download=True, output_template="/tmp/out.%(ext)s")

        self.assertEqual(opts["outtmpl"], "/tmp/out.%(ext)s")
        self.assertEqual(opts["merge_output_format"], "mp4")
        self.assertNotIn("skip_download", opts)

    def test_tiktok_gets_its_extractor_workaround(self) -> None:
        self.assertIn("tiktok", build_ydl_opts(TIKTOK_URL, download=False)["extractor_args"])
        self.assertNotIn("extractor_args", build_ydl_opts(YOUTUBE_URL, download=False))

    def test_existing_cookie_file_is_used(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            cookie_path = handle.name
        self.addCleanup(os.remove, cookie_path)

        with patch("downloader.download.COOKIES_FILE", cookie_path):
            opts = build_ydl_opts(TIKTOK_URL, download=False)

        self.assertEqual(opts["cookiefile"], cookie_path)

    def test_missing_cookie_file_is_ignored(self) -> None:
        with patch("downloader.download.COOKIES_FILE", "/nope/cookies.txt"):
            opts = build_ydl_opts(TIKTOK_URL, download=False)

        self.assertNotIn("cookiefile", opts)

    def test_tiktok_selector_prefers_formats_that_carry_audio(self) -> None:
        selector = get_format_selector(TIKTOK_URL)
        first_choice = selector.split("/")[0]

        self.assertIn("[acodec!=none]", first_choice)
        self.assertIn("[filesize<", first_choice)
        # Отдельная видеодорожка со склейкой — только как запасной вариант.
        self.assertLess(selector.index("[acodec!=none]"), selector.index("+bestaudio"))

    def test_every_selector_caps_the_resolution(self) -> None:
        for url in (TIKTOK_URL, YOUTUBE_URL):
            with self.subTest(url=url):
                self.assertIn("height<=1080", get_format_selector(url))


class MetadataTest(unittest.TestCase):
    def test_compact_counts(self) -> None:
        self.assertEqual(parse_compact_count("1.5K"), 1500)
        self.assertEqual(parse_compact_count("2 M"), 2_000_000)
        self.assertEqual(parse_compact_count("1,234"), 1234)
        self.assertIsNone(parse_compact_count("many"))

    def test_dimensions_fall_back_to_the_first_format(self) -> None:
        self.assertEqual(
            pick_video_dimensions({"formats": [{"width": 720, "height": 1280}]}), (720, 1280)
        )

    def test_top_level_dimensions_win(self) -> None:
        meta = {"width": 1080, "height": 1920, "formats": [{"width": 1, "height": 1}]}
        self.assertEqual(pick_video_dimensions(meta), (1080, 1920))

    def test_missing_fields_get_readable_placeholders(self) -> None:
        info = normalize_video_info(YOUTUBE_URL, {}, 0)

        self.assertEqual(info.title, "Без названия")
        self.assertEqual(info.uploader, "Неизвестно")
        self.assertIsNone(info.view_count)

    def test_counts_are_picked_from_alternative_keys(self) -> None:
        info = normalize_video_info(
            TIKTOK_URL, {"play_count": "1.2K", "repost_count": 7, "channel": "creator"}, 10
        )

        self.assertEqual(info.view_count, 1200)
        self.assertEqual(info.like_count, 7)

    def test_generated_tiktok_title_is_replaced_by_the_description(self) -> None:
        info = normalize_video_info(
            TIKTOK_URL,
            {"title": "TikTok video #7234567890", "description": "Настоящее описание"},
            10,
        )

        self.assertEqual(info.title, "Настоящее описание")

    def test_real_tiktok_title_is_kept(self) -> None:
        info = normalize_video_info(
            TIKTOK_URL, {"title": "Кот и холодильник", "description": "..."}, 10
        )

        self.assertEqual(info.title, "Кот и холодильник")


class FetchInfoTest(unittest.TestCase):
    """`fetch_info` — единственный фильтр: пропускает только короткие вертикальные."""

    def fetch(self, meta: dict, url: str = YOUTUBE_URL):
        with patch("downloader.download._extract_with_retries", return_value=meta):
            return fetch_info(url)

    def test_short_vertical_video_is_accepted(self) -> None:
        info = self.fetch(video_meta(duration=30))

        self.assertEqual((info.width, info.height), (720, 1280))
        self.assertEqual(info.duration, 30)

    def test_horizontal_video_is_rejected_before_the_duration_check(self) -> None:
        # Длинное горизонтальное видео должно объясняться ориентацией,
        # а не длительностью — так понятнее пользователю.
        meta = video_meta(duration=1128, width=1920, height=1080)
        with self.assertRaises(UnsupportedContentError):
            self.fetch(meta)

    def test_long_vertical_video_is_rejected_as_too_long(self) -> None:
        with self.assertRaises(VideoTooLongError):
            self.fetch(video_meta(duration=1128))

    def test_playlist_or_profile_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedContentError):
            self.fetch({"_type": "playlist", "entries": [{"id": "a"}]}, TIKTOK_URL)

    def test_live_stream_is_rejected(self) -> None:
        for live_status in ("is_live", "is_upcoming", "was_live", "post_live"):
            with self.subTest(live_status=live_status):
                with self.assertRaises(UnsupportedContentError):
                    self.fetch(video_meta(live_status=live_status))

    def test_photo_post_is_rejected(self) -> None:
        meta = video_meta(formats=[{"vcodec": "none"}, {"vcodec": None}])
        with self.assertRaises(UnsupportedContentError):
            self.fetch(meta)

    def test_photo_link_error_gets_a_friendly_message(self) -> None:
        error = yt_dlp.utils.DownloadError("Unsupported URL: https://tiktok.com/@u/photo/1")
        with patch("downloader.download._extract_with_retries", side_effect=error):
            with self.assertRaises(UnsupportedContentError):
                fetch_info("https://www.tiktok.com/@u/photo/1")

    def test_other_ytdlp_errors_surface_as_download_errors(self) -> None:
        error = yt_dlp.utils.DownloadError("Video unavailable")
        with patch("downloader.download._extract_with_retries", side_effect=error):
            with self.assertRaises(DownloadError):
                fetch_info(YOUTUBE_URL)


class RetryTest(unittest.TestCase):
    def _extract(self, side_effect):
        with patch("downloader.download.yt_dlp.YoutubeDL") as ydl_class:
            ydl = ydl_class.return_value.__enter__.return_value
            ydl.extract_info.side_effect = side_effect
            with patch("downloader.download.time.sleep"):
                result = _extract_with_retries(YOUTUBE_URL, {}, download=False)
            return result, ydl.extract_info.call_count

    def test_transient_failure_is_retried(self) -> None:
        result, calls = self._extract(
            [yt_dlp.utils.DownloadError("Connection reset by peer"), {"id": "ok"}]
        )

        self.assertEqual(result, {"id": "ok"})
        self.assertEqual(calls, 2)

    def test_permanent_failure_is_not_retried(self) -> None:
        with self.assertRaises(yt_dlp.utils.DownloadError):
            self._extract([yt_dlp.utils.DownloadError("Video unavailable")])

    def test_retries_eventually_give_up(self) -> None:
        with self.assertRaises(yt_dlp.utils.DownloadError):
            self._extract(yt_dlp.utils.DownloadError("Read timed out"))

    def test_dns_failures_get_an_actionable_message(self) -> None:
        rewritten = _rewrite_download_error("ERROR: getaddrinfo failed")

        self.assertIn("DNS", rewritten)
        self.assertNotIn("getaddrinfo", rewritten)

    def test_unrelated_errors_are_passed_through(self) -> None:
        self.assertEqual(_rewrite_download_error("Video unavailable"), "Video unavailable")


class AudioProbeTest(unittest.TestCase):
    def _probe(self, stdout: str):
        from types import SimpleNamespace

        with patch(
            "downloader.download.subprocess.run",
            return_value=SimpleNamespace(stdout=stdout),
        ):
            return _file_has_audio_stream("/tmp/video.mp4")

    def test_detects_an_audio_stream(self) -> None:
        self.assertTrue(self._probe('{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}'))

    def test_detects_a_silent_file(self) -> None:
        self.assertFalse(self._probe('{"streams":[{"codec_type":"video"}]}'))

    def test_unreadable_output_is_inconclusive(self) -> None:
        # None ≠ False: без ffprobe нельзя утверждать, что звука нет.
        self.assertIsNone(self._probe("not json"))

    def test_missing_ffprobe_is_inconclusive(self) -> None:
        with patch("downloader.download.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(_file_has_audio_stream("/tmp/video.mp4"))


class DownloadVideoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.out_dir = self._tempdir.name

    def run_download(
        self, *, url=YOUTUBE_URL, meta=None, payload=b"video", has_audio=True, max_size_mb=50
    ):
        """Запускает `download_video` с подменённым извлечением и временной папкой."""
        meta = meta if meta is not None else video_meta()

        def fake_extract(_url, ydl_opts, *, download):
            if download:
                path = ydl_opts["outtmpl"].replace("%(title)s", "clip").replace("%(ext)s", "mp4")
                Path(path).write_bytes(payload)
            return meta

        with ExitStack() as stack:
            stack.enter_context(patch("downloader.download.DOWNLOAD_DIR", self.out_dir))
            stack.enter_context(patch("downloader.download.MAX_FILE_SIZE_MB", max_size_mb))
            stack.enter_context(
                patch("downloader.download._extract_with_retries", side_effect=fake_extract)
            )
            stack.enter_context(
                patch("downloader.download._file_has_audio_stream", return_value=has_audio)
            )
            return download_video(url)

    def remaining_files(self) -> list[str]:
        return os.listdir(self.out_dir)

    def test_successful_download_returns_the_file_and_metadata(self) -> None:
        result = self.run_download(meta=video_meta(title="Кот", uploader="catlover"))

        self.assertTrue(os.path.exists(result.file_path))
        self.assertEqual(result.info.title, "Кот")
        self.assertEqual(result.info.uploader, "catlover")
        self.assertEqual(result.info.duration, 30)

    def test_progress_callback_receives_percentages(self) -> None:
        seen: list[float] = []

        def fake_extract(_url, ydl_opts, *, download):
            hook = ydl_opts["progress_hooks"][0]
            hook({"status": "downloading", "downloaded_bytes": 25, "total_bytes": 100})
            hook({"status": "finished"})
            path = ydl_opts["outtmpl"].replace("%(title)s", "clip").replace("%(ext)s", "mp4")
            Path(path).write_bytes(b"video")
            return video_meta()

        with ExitStack() as stack:
            stack.enter_context(patch("downloader.download.DOWNLOAD_DIR", self.out_dir))
            stack.enter_context(
                patch("downloader.download._extract_with_retries", side_effect=fake_extract)
            )
            stack.enter_context(
                patch("downloader.download._file_has_audio_stream", return_value=True)
            )
            download_video(YOUTUBE_URL, on_progress=seen.append)

        self.assertEqual(seen, [25.0])

    def test_oversized_file_is_rejected_and_deleted(self) -> None:
        with self.assertRaises(FileTooLargeError):
            self.run_download(max_size_mb=0)

        self.assertEqual(self.remaining_files(), [])

    def test_silent_tiktok_is_rejected_and_deleted(self) -> None:
        with self.assertRaises(DownloadError):
            self.run_download(url=TIKTOK_URL, has_audio=False)

        self.assertEqual(self.remaining_files(), [])

    def test_silent_youtube_video_is_kept(self) -> None:
        # Проверка на звук нужна только TikTok, где встречаются немые склейки.
        result = self.run_download(url=YOUTUBE_URL, has_audio=False)

        self.assertTrue(os.path.exists(result.file_path))

    def test_video_over_the_hard_duration_limit_is_rejected(self) -> None:
        with patch("downloader.download.MAX_DURATION_SEC", 60):
            with self.assertRaises(VideoTooLongError):
                self.run_download(meta=video_meta(duration=120))

    def test_missing_output_file_is_reported(self) -> None:
        def fake_extract(_url, _opts, *, download):
            return video_meta()

        with ExitStack() as stack:
            stack.enter_context(patch("downloader.download.DOWNLOAD_DIR", self.out_dir))
            stack.enter_context(
                patch("downloader.download._extract_with_retries", side_effect=fake_extract)
            )
            with self.assertRaises(DownloadError):
                download_video(YOUTUBE_URL)

    def test_cleanup_removes_the_file_and_tolerates_a_missing_one(self) -> None:
        path = os.path.join(self.out_dir, "clip.mp4")
        Path(path).write_bytes(b"x")

        cleanup(path)
        cleanup(path)

        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
