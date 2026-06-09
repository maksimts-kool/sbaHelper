from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VideoInfo:
    title: str
    uploader: str
    duration: int
    thumbnail: str | None
    view_count: int | None
    like_count: int | None
    width: int | None = None
    height: int | None = None


@dataclass
class DownloadResult:
    file_path: str
    info: VideoInfo
