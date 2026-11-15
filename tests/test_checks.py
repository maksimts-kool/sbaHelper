import logging
from pathlib import Path

import pytest

from sbahelper import checks, download
from sbahelper.config import settings
from sbahelper.db import Database
from sbahelper.download import DownloadError, DownloadResult, VideoInfo


def fake_result(tmp_path: Path) -> DownloadResult:
    workdir = download.make_workdir(tmp_path)
    path = workdir / "video.mp4"
    path.write_bytes(b"x" * 1_000_000)
    return DownloadResult(path, VideoInfo("tiktok", "Кот", "cat", 14), path.stat().st_size, workdir)


def test_passing_check_reports_the_video_and_cleans_up(monkeypatch, tmp_path: Path) -> None:
    result = fake_result(tmp_path)
    monkeypatch.setattr(checks, "download_video", lambda url: result)

    outcome = checks.run_check("tiktok", "https://www.tiktok.com/@u/video/1")

    assert outcome.ok
    assert outcome.detail.startswith("Кот (1.0 MB, ")
    assert not result.path.parent.exists()


def test_failed_check_is_logged_as_an_error(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    def fail(url):
        raise DownloadError("Video unavailable")

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
    monkeypatch.setattr(checks, "run_check", lambda platform, url, db=None: platform)
    assert checks.run_checks() == ["youtube"]


def test_last_results_are_kept_for_status(monkeypatch, tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    monkeypatch.setattr(settings, "check_urls", {"tiktok": "u", "youtube": "v"})
    monkeypatch.setattr(checks, "download_video", lambda url: fake_result(tmp_path))
    checks.run_check("tiktok", "https://www.tiktok.com/@u/video/1", db)

    results = checks.last_results(db)

    assert set(results) == {"tiktok"} and results["tiktok"][0] is True


def test_check_logs_do_not_leak_signed_urls(monkeypatch, caplog) -> None:
    def fail(url):
        raise DownloadError("HTTP Error 403 at https://cdn.example/v.mp4?sig=SECRET&expire=1")

    monkeypatch.setattr(checks, "download_video", fail)
    outcome = checks.run_check("youtube", "https://youtu.be/x?si=tracking")

    assert "SECRET" not in outcome.detail
    assert "SECRET" not in caplog.text and "tracking" not in caplog.text
