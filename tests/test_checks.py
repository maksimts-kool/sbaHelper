import logging
from pathlib import Path

import pytest

from sbahelper import checks
from sbahelper.config import settings
from sbahelper.download import DownloadError, DownloadResult, VideoInfo


def fake_result(tmp_path: Path) -> DownloadResult:
    workdir = tmp_path / "work"
    workdir.mkdir()
    path = workdir / "video.mp4"
    path.write_bytes(b"x" * 1024 * 1024)
    return DownloadResult(path, VideoInfo("tiktok", "Кот", "cat", 14), path.stat().st_size)


def test_passing_check_reports_the_video_and_cleans_up(monkeypatch, tmp_path: Path) -> None:
    result = fake_result(tmp_path)
    monkeypatch.setattr(checks, "download_video", lambda url: result)

    outcome = checks.run_check("tiktok", "https://www.tiktok.com/@u/video/1")

    assert outcome.ok
    assert outcome.detail.startswith("Кот (1.0 MB, ")
    assert not result.path.parent.exists()


def test_failed_check_is_logged_as_an_error(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    def fail(url):
        raise DownloadError("<b>chat text</b>", "Video unavailable")

    monkeypatch.setattr(checks, "download_video", fail)
    with caplog.at_level(logging.INFO):
        outcome = checks.run_check("youtube", "https://youtu.be/x")

    assert (outcome.ok, outcome.detail) == (False, "Video unavailable")
    assert caplog.records[-1].levelno == logging.ERROR


def test_crash_is_reported_not_raised(monkeypatch) -> None:
    def crash(url):
        raise KeyError("formats")

    monkeypatch.setattr(checks, "download_video", crash)
    outcome = checks.run_check("youtube", "https://youtu.be/x")
    assert not outcome.ok and "KeyError" in outcome.detail


def test_only_configured_platforms_are_checked(monkeypatch) -> None:
    monkeypatch.setattr(settings, "check_urls", {"tiktok": "", "youtube": "https://youtu.be/x"})
    monkeypatch.setattr(checks, "run_check", lambda platform, url: platform)
    assert checks.run_checks() == ["youtube"]
