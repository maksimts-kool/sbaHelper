"""Media inspection. Parsing tests are pure; the rest use real ffmpeg when installed
(CI installs it) and skip otherwise, with synthetic one-second clips."""

import shutil
import subprocess
from pathlib import Path

import pytest

from sbahelper import media

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)


def make_clip(path: Path, *, size: str = "180x320", audio: bool = True, faststart: bool = True):
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={size}:duration=1:rate=10",
    ]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest"]
    if faststart:
        args += ["-movflags", "+faststart"]
    subprocess.run([*args, str(path)], check=True, timeout=60)
    return path


def test_probe_output_is_parsed_with_rotation() -> None:
    info = media.parse_probe(
        {
            "format": {"format_name": "mov,mp4,m4a", "duration": "12.5"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "side_data_list": [{"rotation": -90}],
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
    )
    assert (info.width, info.height, info.duration) == (1080, 1920, 12.5)
    assert info.is_mp4 and info.has_video and info.has_audio


def test_missing_streams() -> None:
    info = media.parse_probe({"format": {"format_name": "matroska,webm"}, "streams": []})
    assert not info.has_video and not info.has_audio and not info.is_mp4


def test_missing_tool_is_reported(monkeypatch, tmp_path: Path) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(media.subprocess, "run", missing)
    with pytest.raises(media.ToolMissing):
        media.probe(tmp_path / "x.mp4")


@needs_ffmpeg
def test_real_vertical_clip(tmp_path: Path) -> None:
    info = media.probe(make_clip(tmp_path / "v.mp4"))
    assert (info.width, info.height) == (180, 320)
    assert info.video_codec == "h264" and info.has_audio
    assert 0.8 < info.duration < 1.5
    assert media.moov_after_mdat(tmp_path / "v.mp4") is False


@needs_ffmpeg
def test_truncated_file_fails_to_probe(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "v.mp4", faststart=False)
    data = clip.read_bytes()
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(data[: len(data) // 3])
    with pytest.raises(media.ProbeFailed):
        media.probe(broken)


@needs_ffmpeg
def test_index_at_the_end_is_moved_to_the_front(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "late.mp4", faststart=False)
    assert media.moov_after_mdat(clip) is True

    fixed = media.make_streamable(clip, media.probe(clip))

    assert fixed != clip and not clip.exists()
    assert media.moov_after_mdat(fixed) is False
    assert media.probe(fixed).has_video


def test_garbage_is_not_mp4(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00\x00\x00\x02xx")
    assert media.moov_after_mdat(junk) is None
