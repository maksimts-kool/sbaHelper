import os
import time
from pathlib import Path

from sbahelper import health
from sbahelper.config import settings


def test_fresh_heartbeat_is_healthy(tmp_path: Path) -> None:
    path = tmp_path / "hb"
    health.beat(path)
    assert health.check_heartbeat(path)[0]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".heartbeat-")]


def test_old_or_missing_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    assert health.check_heartbeat(tmp_path / "missing") == (
        False,
        f"no heartbeat at {tmp_path / 'missing'}",
    )
    path = tmp_path / "hb"
    path.write_text(f"{time.time() - 500:.0f}\n")
    ok, detail = health.check_heartbeat(path, max_age=120)
    assert not ok and "old" in detail
    path.write_text("garbage")
    assert not health.check_heartbeat(path)[0]


def test_cli_exit_codes(capsys) -> None:
    assert health.main([]) == 1
    health.beat(settings.heartbeat_path)
    assert health.main([]) == 0
    assert capsys.readouterr().out.startswith("FAIL")


def test_dependency_check_reports_missing_tools(monkeypatch) -> None:
    monkeypatch.setattr(health.shutil, "which", lambda tool: None)
    problems = health.check_dependencies()
    assert {
        "ffmpeg not found on PATH",
        "ffprobe not found on PATH",
        "deno not found on PATH",
    } <= set(problems)


def test_heartbeat_file_is_not_world_writable(tmp_path: Path) -> None:
    old = os.umask(0o077)
    try:
        health.beat(tmp_path / "hb")
    finally:
        os.umask(old)
    assert (tmp_path / "hb").stat().st_mode & 0o022 == 0
