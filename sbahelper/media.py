"""What is actually in a downloaded file, and making it fit for Telegram.

yt-dlp's metadata describes what the site *offered*. This module looks at the
file that arrived: ffprobe for streams, duration and dimensions, and the MP4
box layout for streamability. Nothing here trusts the file extension, and
`merge_output_format="mp4"` only changes the container, never the codecs.

Every external tool runs with an argument list (no shell) and a timeout;
`subprocess.run` kills the child when the timeout expires.
"""

from __future__ import annotations

import json
import logging
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PROBE_TIMEOUT_SEC = 20
REMUX_TIMEOUT_SEC = 90
REQUIRED_TOOLS = ("ffmpeg", "ffprobe")
_MP4_FORMATS = ("mp4", "mov")


class ToolMissing(RuntimeError):
    """ffmpeg or ffprobe is not installed or cannot be run."""


class ProbeFailed(RuntimeError):
    """ffprobe could not read the file: it is incomplete or not media at all."""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    format_names: tuple[str, ...]
    duration: float
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def is_mp4(self) -> bool:
        return any(name in _MP4_FORMATS for name in self.format_names)


def missing_tools() -> list[str]:
    return [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, check=True, text=True, timeout=timeout)
    except FileNotFoundError as error:
        raise ToolMissing(f"{args[0]} not found") from error
    except PermissionError as error:
        raise ToolMissing(f"{args[0]} cannot be executed") from error


def probe(path: Path, timeout: float = PROBE_TIMEOUT_SEC) -> MediaInfo:
    """Streams, duration and display size of `path`."""
    try:
        result = _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration:stream=codec_type,codec_name,width,height,"
                "duration:stream_side_data=rotation:stream_tags=rotate",
                "-of",
                "json",
                str(path),
            ],
            timeout,
        )
        data = json.loads(result.stdout or "{}")
    except subprocess.TimeoutExpired as error:
        raise ProbeFailed("ffprobe timed out") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip().splitlines()
        raise ProbeFailed(detail[-1][:200] if detail else "ffprobe failed") from error
    except json.JSONDecodeError as error:
        raise ProbeFailed("unreadable ffprobe output") from error
    return parse_probe(data)


def _float(value: object) -> float:
    try:
        return float(str(value))
    except TypeError, ValueError:
        return 0.0


def _rotation(stream: dict) -> int:
    for item in stream.get("side_data_list") or []:
        if "rotation" in item:
            return int(_float(item["rotation"])) % 360
    return int(_float((stream.get("tags") or {}).get("rotate"))) % 360


def parse_probe(data: dict) -> MediaInfo:
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    duration = _float(fmt.get("duration")) or max(
        (_float(s.get("duration")) for s in streams), default=0.0
    )
    width = height = None
    if video and video.get("width") and video.get("height"):
        width, height = int(video["width"]), int(video["height"])
        if _rotation(video) in (90, 270):  # phones store portrait as rotated landscape
            width, height = height, width
    names = tuple(n for n in str(fmt.get("format_name") or "").split(",") if n)
    return MediaInfo(
        format_names=names,
        duration=duration,
        width=width,
        height=height,
        video_codec=(video.get("codec_name") or "unknown") if video else None,
        audio_codec=(audio.get("codec_name") or "unknown") if audio else None,
    )


def moov_after_mdat(path: Path) -> bool | None:
    """True when the MP4 index (`moov`) sits after the data (`mdat`).

    Such files only start playing after a full download. None when the
    top-level boxes cannot be walked (not MP4, truncated).
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            offset = 0
            seen_mdat = False
            while offset + 8 <= size:
                handle.seek(offset)
                header = handle.read(16)
                box_size, kind = struct.unpack(">I4s", header[:8])
                if box_size == 1 and len(header) >= 16:
                    box_size = struct.unpack(">Q", header[8:16])[0]
                elif box_size == 0:
                    box_size = size - offset
                if box_size < 8:
                    return None
                if kind == b"moov":
                    return seen_mdat
                if kind == b"mdat":
                    seen_mdat = True
                offset += box_size
    except OSError, struct.error:
        return None
    return None


def remux_mp4(source: Path, target: Path, timeout: float = REMUX_TIMEOUT_SEC) -> None:
    """Copy the streams into an MP4 with the index first (no re-encoding)."""
    try:
        _run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(target),
            ],
            timeout,
        )
    except subprocess.TimeoutExpired as error:
        target.unlink(missing_ok=True)
        raise ProbeFailed("ffmpeg remux timed out") from error
    except subprocess.CalledProcessError as error:
        target.unlink(missing_ok=True)
        detail = (error.stderr or "").strip().splitlines()
        raise ProbeFailed(detail[-1][:200] if detail else "ffmpeg remux failed") from error


def make_streamable(path: Path, info: MediaInfo) -> Path:
    """An MP4 whose index comes first: `path` itself, or a remuxed copy next to it.

    A file that cannot be remuxed is returned unchanged: it still plays after a
    full download, which is how it was always sent.
    """
    if info.is_mp4 and moov_after_mdat(path) is not True:
        return path
    target = path.with_name(f"{path.stem}.fast.mp4")
    try:
        remux_mp4(path, target)
    except ProbeFailed as error:
        log.warning("Remux skipped. File=%s Error=%s", path.name, error)
        return path
    path.unlink(missing_ok=True)
    return target
