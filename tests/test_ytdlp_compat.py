"""Checks against the installed yt-dlp, not fakes: the private APIs we override,
how our selectors actually choose, and that the progress hook really stops a
transfer. The download tests use a local HTTP server, never the internet."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

from sbahelper import download
from sbahelper.config import settings


def test_private_impersonation_apis_still_exist() -> None:
    with download._YoutubeDL({"quiet": True}, targets=("chrome-131",)) as ydl:
        _, chosen = ydl._parse_impersonate_targets(True)
        assert [str(t) for t in chosen] == ["chrome-131"]
        assert isinstance(
            ydl._impersonate_target_available(ImpersonateTarget.from_str("chrome-131")), bool
        )


def test_download_cancelled_exists() -> None:
    assert issubclass(download._Abort, yt_dlp.utils.DownloadCancelled)


def choose(selector: str, formats: list[dict]) -> list[str]:
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        select = ydl.build_format_selector(selector)
        ctx = {"formats": formats, "has_merged_format": False, "incomplete_formats": False}
        return [chosen["format_id"] for chosen in select(ctx)]


def fmt(format_id: str, **fields) -> dict:
    base = {
        "format_id": format_id,
        "ext": "mp4",
        "url": "http://x/" + format_id,
        "protocol": "https",
    }
    return {**base, **fields}


def test_tiktok_selector_limit_is_exact_bytes() -> None:
    selector = download.format_selector("tiktok", 45_000_000)
    common = {"vcodec": "h264", "acodec": "aac", "width": 1080, "height": 1920}
    formats = [
        fmt("small", filesize=44_999_999, **common),
        fmt("big", filesize=45_000_001, **common),  # bigger, so yt-dlp would prefer it
    ]
    assert choose(selector, formats) == ["small"]


def test_tiktok_selector_prefers_files_with_audio() -> None:
    selector = download.format_selector("tiktok", 45_000_000)
    formats = [
        fmt("silent", vcodec="h264", acodec="none", width=1080, height=1920, filesize=1_000_000),
        fmt("muxed", vcodec="h264", acodec="aac", width=720, height=1280, filesize=900_000),
    ]
    assert choose(selector, formats) == ["muxed"]


def test_youtube_selector_caps_the_short_side_and_size() -> None:
    selector = download.format_selector("youtube", 45_000_000)
    formats = [
        fmt(
            "1440",
            vcodec="avc1",
            acodec="none",
            width=1440,
            height=2560,
            filesize_approx=30_000_000,
        ),
        fmt(
            "1080big",
            vcodec="avc1",
            acodec="none",
            width=1080,
            height=1920,
            filesize_approx=41_000_000,
        ),
        fmt(
            "1080",
            vcodec="avc1",
            acodec="none",
            width=1080,
            height=1920,
            filesize_approx=20_000_000,
            tbr=1,
        ),
        fmt("audio", ext="m4a", vcodec="none", acodec="mp4a", filesize=1_000_000),
    ]
    chosen = choose(selector, formats)
    assert "1080" in chosen[0] and chosen[0] != "1080big" and "1440" not in chosen[0]


# --------------------------------------------------------------------------- #
#  Real transfers from a local server                                         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    payload = tmp_path / "srv"
    payload.mkdir()
    (payload / "video.mp4").write_bytes(b"\0" * 3_000_000)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(payload), **kwargs)

        def log_message(self, *args) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/video.mp4"
    httpd.shutdown()


def transfer(url: str, workdir: Path, cancel: download.Cancel | None = None) -> None:
    """Our options and hook, on a direct-URL info dict (no extractor involved)."""
    opts = download._options("tiktok", workdir, None, None, cancel)
    opts["http_chunk_size"] = 65_536
    info = {"id": "local", "title": "local", "url": url, "ext": "mp4", "extractor": "test",
            "extractor_key": "Test", "webpage_url": url}  # fmt: skip
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ydl.process_ie_result(info, download=True)
        except download._Abort as abort:
            raise abort.error from None


def test_real_transfer_within_the_limit(server: str, tmp_path: Path) -> None:
    transfer(server, tmp_path)
    assert (tmp_path / "video.mp4").stat().st_size == 3_000_000


def test_real_transfer_is_stopped_at_the_byte_limit(
    server: str, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "max_file_size_bytes", 1_000_000)
    with pytest.raises(download.Rejected) as caught:
        transfer(server, tmp_path)
    assert caught.value.code == "too_big"
    assert not (tmp_path / "video.mp4").exists()


def test_real_transfer_is_cancelled(server: str, tmp_path: Path) -> None:
    cancel = download.Cancel()
    cancel.cancel("shutdown")
    with pytest.raises(download.Cancelled):
        transfer(server, tmp_path, cancel)
    assert not (tmp_path / "video.mp4").exists()


def test_generic_extractor_is_not_available(tmp_path: Path) -> None:
    opts = download._options("tiktok", tmp_path, None, None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        suitable = [
            name for name, ie in ydl._ies.items() if ie.suitable("https://example.com/v.mp4")
        ]
    assert suitable == []
    with pytest.raises(yt_dlp.utils.DownloadError) as caught, yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info("https://example.com/v.mp4", download=False)
    assert isinstance(
        download.user_error(str(caught.value), "https://example.com"), download.Rejected
    )
