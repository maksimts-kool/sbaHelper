"""Container healthcheck and dependency self-check.

    python -m sbahelper.health            # exit 0 if the heartbeat is fresh
    python -m sbahelper.health --deps     # ffmpeg/ffprobe/deno/yt-dlp integration points

The running bot rewrites HEARTBEAT_PATH every 30 s from its JobQueue, so a
fresh file proves the event loop and scheduler are alive (a hung loop stops
beating). The check reads one small file: no Telegram call, no download, no
credentials.

Docker marks a container "unhealthy" after the configured retries; that by
itself restarts nothing. Restarts come from `restart:` policies (which react
to the process exiting) or an orchestrator / autoheal tool that watches health.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

MAX_AGE_SEC = 120.0


def beat(path: Path) -> None:
    """Atomically replace the heartbeat file with the current time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".heartbeat-")
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(f"{time.time():.0f}\n")
    os.replace(tmp, path)


def age(path: Path, now: float | None = None) -> float | None:
    try:
        stamp = float(path.read_text(encoding="ascii").strip())
    except OSError, ValueError:
        return None
    return (now if now is not None else time.time()) - stamp


def check_heartbeat(path: Path, max_age: float = MAX_AGE_SEC) -> tuple[bool, str]:
    seconds = age(path)
    if seconds is None:
        return False, f"no heartbeat at {path}"
    if seconds > max_age:
        return False, f"heartbeat is {seconds:.0f}s old (limit {max_age:.0f}s)"
    return True, f"heartbeat {seconds:.0f}s old"


def check_dependencies() -> list[str]:
    """Problems with the tools and private yt-dlp APIs the bot relies on."""
    problems = [
        f"{tool} not found on PATH"
        for tool in ("ffmpeg", "ffprobe", "deno")
        if not shutil.which(tool)
    ]
    try:
        import yt_dlp
        from yt_dlp.networking.impersonate import ImpersonateTarget

        from sbahelper import download

        with download._YoutubeDL({"quiet": True}, targets=("chrome-131",)) as ydl:
            _, targets = ydl._parse_impersonate_targets(True)
            if [str(t) for t in targets] != ["chrome-131"]:
                problems.append("yt-dlp _parse_impersonate_targets override has no effect")
            ydl._impersonate_target_available(ImpersonateTarget.from_str("chrome-131"))
            ydl.build_format_selector(download.format_selector("tiktok"))
            ydl.build_format_selector(download.format_selector("youtube"))
        if not download.impersonate_targets():
            problems.append("no usable curl-cffi impersonation target")
        if not hasattr(yt_dlp.utils, "DownloadCancelled"):
            problems.append("yt_dlp.utils.DownloadCancelled is missing")
    except Exception as error:
        problems.append(f"yt-dlp integration check failed: {type(error).__name__}: {error}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sbahelper.health")
    parser.add_argument("--deps", action="store_true", help="check tools and yt-dlp integration")
    parser.add_argument("--max-age", type=float, default=MAX_AGE_SEC)
    args = parser.parse_args(argv)
    if args.deps:
        problems = check_dependencies()
        for problem in problems:
            print(f"FAIL {problem}")
        if not problems:
            print("OK dependencies")
        return 1 if problems else 0
    from sbahelper.config import settings

    ok, detail = check_heartbeat(settings.heartbeat_path, args.max_age)
    print(("OK " if ok else "FAIL ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
