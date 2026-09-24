"""The yt-dlp engine. yt-dlp itself is replaced by fakes: no network, no ffmpeg."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yt_dlp

from sbahelper import download
from sbahelper.config import settings
from sbahelper.download import (
    Blocked,
    DownloadError,
    LoginRequired,
    Rejected,
    clean_error,
    download_video,
    format_selector,
    parse_count,
    user_error,
    validate,
    video_info,
)

TIKTOK = "https://www.tiktok.com/@user/video/123"
YOUTUBE = "https://youtube.com/shorts/abc123"


def meta(**overrides) -> dict:
    return {
        "title": "Clip",
        "uploader": "Author",
        "duration": 30,
        "width": 720,
        "height": 1280,
        "formats": [{"vcodec": "h264", "width": 720, "height": 1280}],
        **overrides,
    }


# --------------------------------------------------------------------------- #
#  Options                                                                    #
# --------------------------------------------------------------------------- #


def test_selectors_cap_the_short_side_and_the_size() -> None:
    for platform in ("tiktok", "youtube"):
        selector = format_selector(platform)
        assert "[width<=?1080]" in selector
        assert "height<=" not in selector  # vertical video: height is the long side
        assert selector.endswith("/b")
    assert "[filesize<50M]" in format_selector("tiktok")
    assert "[filesize_approx<45M]" in format_selector("youtube")


def test_tiktok_prefers_files_that_already_have_audio() -> None:
    selector = format_selector("tiktok")
    assert "[acodec!=none]" in selector.split("/")[0]
    assert selector.index("[acodec!=none]") < selector.index("+ba")


def test_youtube_prefers_h264() -> None:
    assert "[vcodec^=avc]" in format_selector("youtube").split("/")[0]


def test_options_wire_cookies_and_progress(tmp_path: Path) -> None:
    seen: list[float] = []
    opts = download._options("tiktok", tmp_path, "/c/tiktok.txt", seen.append)
    hook = opts["progress_hooks"][0]

    hook({"status": "downloading", "downloaded_bytes": 25, "total_bytes": 100})
    hook({"status": "downloading", "downloaded_bytes": 5})  # no total yet
    hook({"status": "finished"})

    assert opts["cookiefile"] == "/c/tiktok.txt"
    assert opts["outtmpl"] == str(tmp_path / "video.%(ext)s")
    assert seen == [25.0]


def test_progress_callback_errors_never_break_the_download(tmp_path: Path) -> None:
    def broken(_: float) -> None:
        raise RuntimeError("UI is gone")

    opts = download._options("youtube", tmp_path, None, broken)
    opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})
    assert "cookiefile" not in opts


# --------------------------------------------------------------------------- #
#  Metadata and validation                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1500, 1500), (2.7, 2), ("1.5K", 1500), ("2 M", 2_000_000), ("1,234", 1234), ("many", None)],
)
def test_counts(raw, expected) -> None:
    assert parse_count(raw) == expected


def test_missing_fields_get_placeholders() -> None:
    info = video_info("youtube", {})
    assert (info.title, info.uploader, info.duration, info.view_count) == (
        "Без названия",
        "Неизвестно",
        0,
        None,
    )


def test_tiktok_generated_title_is_replaced_by_the_description() -> None:
    info = video_info(
        "tiktok",
        {
            "title": "TikTok video #7234567890",
            "description": "Кот",
            "channel": "cat",
            "play_count": "1.2K",
        },
    )
    assert (info.title, info.uploader, info.view_count) == ("Кот", "cat", 1200)


def test_dimensions_fall_back_to_the_formats() -> None:
    info = video_info("tiktok", {"formats": [{"width": 576, "height": 1024}]})
    assert (info.width, info.height) == (576, 1024)


def test_short_vertical_video_passes() -> None:
    assert validate("youtube", meta()).duration == 30


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"_type": "playlist", "entries": []}, "profile"),
        ({"live_status": "is_live"}, "live"),
        ({"live_status": "was_live"}, "live"),
        ({"formats": [{"vcodec": "none"}, {"vcodec": None}]}, "photo"),
        # Orientation is checked first: the length is not the real problem here.
        ({"width": 1920, "height": 1080, "duration": 1200}, "horizontal"),
        ({"width": 1080, "height": 1080}, "horizontal"),
        ({"duration": 301}, "too long"),
    ],
)
def test_everything_else_is_rejected(overrides: dict, reason: str) -> None:
    with pytest.raises(Rejected, match=reason):
        validate("youtube", meta(**overrides))


def test_unknown_size_and_length_are_allowed() -> None:
    validate("tiktok", meta(width=None, height=None, duration=None, formats=[]))


# --------------------------------------------------------------------------- #
#  Errors                                                                     #
# --------------------------------------------------------------------------- #


def test_ytdlp_prefixes_are_stripped() -> None:
    assert clean_error("ERROR: [TikTok] 7234: Video unavailable") == "Video unavailable"
    assert clean_error("ERROR: [youtube:tab] abc-1: Private video") == "Private video"


@pytest.mark.parametrize(
    ("message", "kind", "alert"),
    [
        ("ERROR: [TikTok] 1: Unexpected response from webpage request", Blocked, True),
        (
            "ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies",
            LoginRequired,
            True,
        ),
        ("ERROR: getaddrinfo failed", DownloadError, False),
        ("ERROR: Unsupported URL: https://www.tiktok.com/@u/photo/1", Rejected, False),
        ("ERROR: [youtube] x: Video unavailable", DownloadError, False),
    ],
)
def test_errors_are_mapped_for_chat(message: str, kind: type, alert: bool) -> None:
    error = user_error(message, TIKTOK)
    assert type(error) is kind
    assert error.alert is alert
    assert "ERROR:" not in error.reason


def test_unknown_errors_show_the_reason() -> None:
    error = user_error("ERROR: [youtube] x: Video unavailable", YOUTUBE)
    assert "<code>Video unavailable</code>" in error.text


# --------------------------------------------------------------------------- #
#  Extraction retries                                                         #
# --------------------------------------------------------------------------- #


def fake_ydl_factory(monkeypatch, outcomes: list, targets=("chrome-146", "chrome-142")):
    """Each constructed YoutubeDL returns the next outcome from `extract_info`."""
    opened: list[tuple[str, ...]] = []
    monkeypatch.setattr(download, "impersonate_targets", lambda: tuple(targets))
    monkeypatch.setattr(download.time, "sleep", lambda _: None)

    def factory(opts, *, targets=()):
        opened.append(targets)
        ydl = MagicMock()
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            ydl.extract_info.side_effect = outcome
        else:
            ydl.extract_info.return_value = outcome
        return ydl

    monkeypatch.setattr(download, "_YoutubeDL", factory)
    return opened


def blocked() -> yt_dlp.utils.DownloadError:
    return yt_dlp.utils.DownloadError("ERROR: [TikTok] 1: Unexpected response from webpage request")


def test_network_errors_are_retried(monkeypatch) -> None:
    fake_ydl_factory(
        monkeypatch, [yt_dlp.utils.DownloadError("Connection reset by peer"), {"id": 1}]
    )
    _, info = download._extract(TIKTOK, {})
    assert info == {"id": 1}


def test_network_retries_give_up(monkeypatch) -> None:
    errors = [yt_dlp.utils.DownloadError("Read timed out") for _ in range(download.RETRIES)]
    fake_ydl_factory(monkeypatch, errors)
    with pytest.raises(DownloadError) as caught:
        download._extract(TIKTOK, {})
    assert caught.value.text == download.texts.NETWORK


def test_permanent_errors_are_not_retried(monkeypatch) -> None:
    opened = fake_ydl_factory(monkeypatch, [yt_dlp.utils.DownloadError("Video unavailable")])
    with pytest.raises(DownloadError):
        download._extract(TIKTOK, {})
    assert len(opened) == 1


def test_blocked_target_is_swapped_for_the_next(monkeypatch) -> None:
    opened = fake_ydl_factory(monkeypatch, [blocked(), {"id": 1}])
    download._extract(TIKTOK, {})
    assert opened == [("chrome-146", "chrome-142"), ("chrome-142",)]


def test_block_is_reported_when_targets_run_out(monkeypatch) -> None:
    fake_ydl_factory(monkeypatch, [blocked(), blocked()])
    with pytest.raises(Blocked):
        download._extract(TIKTOK, {})


def test_any_target_request_uses_our_preferences() -> None:
    with download._YoutubeDL({"quiet": True}, targets=("chrome-131", "firefox-144")) as ydl:
        _, requested = ydl._parse_impersonate_targets(True)
        _, explicit = ydl._parse_impersonate_targets("safari-18.4")
    assert [str(t) for t in requested] == ["chrome-131", "firefox-144"]
    assert [str(t) for t in explicit] == ["safari-18.4"]


# --------------------------------------------------------------------------- #
#  Audio probe                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}', True),
        ('{"streams":[{"codec_type":"video"}]}', False),
        ("not json", None),  # None ≠ False: without ffprobe we cannot say there is no sound
    ],
)
def test_audio_probe(monkeypatch, stdout: str, expected) -> None:
    monkeypatch.setattr(download.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout))
    assert download.has_audio(Path("video.mp4")) is expected


def test_missing_ffprobe_is_inconclusive(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(download.subprocess, "run", missing)
    assert download.has_audio(Path("video.mp4")) is None


# --------------------------------------------------------------------------- #
#  download_video                                                             #
# --------------------------------------------------------------------------- #


class FakeYDL:
    """Stands in for the YoutubeDL `_extract` returns; "downloads" a file of `payload`."""

    def __init__(self, opts: dict, payload: bytes | None) -> None:
        self.opts = opts
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def process_ie_result(self, info: dict, download: bool) -> None:
        if self.payload is not None:
            Path(self.opts["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(self.payload)


@pytest.fixture
def run(monkeypatch):
    def run_download(url=YOUTUBE, *, info=None, payload=b"video", audio=True, **kwargs):
        seen: dict = {}

        def fake_extract(_url, opts):
            seen["opts"] = opts
            return FakeYDL(opts, payload), info or meta()

        monkeypatch.setattr(download, "_extract", fake_extract)
        monkeypatch.setattr(download, "has_audio", lambda path: audio)
        return download_video(url, **kwargs), seen

    return run_download


def test_successful_download(run) -> None:
    infos = []
    result, _ = run(info=meta(title="Кот"), on_info=infos.append)

    assert result.path.read_bytes() == b"video"
    assert result.size_bytes == 5
    assert result.info.title == "Кот" and result.info.platform == "youtube"
    assert infos == [result.info]
    result.cleanup()
    assert not result.path.parent.exists()


def test_platform_cookies_are_used(run) -> None:
    settings.cookies_dir.mkdir()
    (settings.cookies_dir / "tiktok.txt").write_text(".tiktok.com\tTRUE\t/\tTRUE\t0\ta\tb\n")

    result, seen = run(TIKTOK)

    assert Path(seen["opts"]["cookiefile"]).parent == settings.cookies_dir
    result.cleanup()


def workdirs() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("sbahelper-*")}


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"info": meta(width=1920, height=1080)}, Rejected),
        ({"payload": None}, DownloadError),
        ({"url": TIKTOK, "audio": False}, DownloadError),
    ],
)
def test_failures_leave_no_files(run, kwargs: dict, error: type) -> None:
    before = workdirs()
    with pytest.raises(error):
        run(**kwargs)
    assert workdirs() == before


def test_oversized_file_is_rejected(run, monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_file_size_mb", 1)
    with pytest.raises(Rejected, match=r"too big 2\.0MB"):
        run(payload=b"x" * 2 * 1024 * 1024)


def test_silent_youtube_video_is_fine(run) -> None:
    # Only TikTok produces silent merges; YouTube videos may legitimately be silent.
    result, _ = run(audio=False)
    result.cleanup()
