"""yt-dlp engine: extract once, validate, then download from that same info.

One extraction per link keeps TikTok requests (and their bot detection) to a
minimum. Everything platform-specific lives here: format selectors, browser
impersonation targets, metadata cleanup and the rules that keep the bot to
short vertical videos. Errors carry a message ready to show in chat.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

from sbahelper import cookies, texts
from sbahelper.config import settings
from sbahelper.links import platform_of

log = logging.getLogger(__name__)

# Quick in-process retries for flaky networks. There is no delayed retry: a
# link that fails here is reported and forgotten.
RETRIES = 3
RETRY_MAX_SLEEP_SEC = 8.0

# TikTok's extractor asks for "any" impersonation target and yt-dlp would pick
# the newest one curl_cffi offers. TikTok's WAF blocks exactly that one with a
# ~500-byte "Site Maintenance" page, while slightly older targets work. So the
# bot picks targets itself, newest known-good first, and moves down the list
# when a request is blocked. Update this tuple when TikTok blocks them all.
IMPERSONATE_TARGETS = (
    "chrome-146",
    "chrome-145",
    "chrome-142",
    "chrome-136",
    "chrome-133",
    "chrome-131",
    "firefox-144",
    "safari-18.4",
    "edge-101",
)

_DNS_ERRORS = (
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "failed to resolve",
    "getaddrinfo failed",
)
_NETWORK_ERRORS = (
    *_DNS_ERRORS,
    "transporterror",
    "network is unreachable",
    "connection reset by peer",
    "connection aborted",
    "connection refused",
    "remote end closed connection without response",
    "server disconnected",
    "timed out",
    "timeout",
)
# TikTok's WAF page instead of the video page: fixed by another fingerprint, not by waiting.
_BLOCKED_ERRORS = (
    "unexpected response from webpage request",
    "unable to extract challenge data",
)
_LOGIN_ERRORS = (
    "sign in to confirm",
    "use --cookies",
    "login required",
    "log in for access",
    "requires authentication",
    "this video is only available for registered users",
)
_LIVE_STATUSES = ("is_live", "is_upcoming", "was_live", "post_live")
_TEMP_SUFFIXES = (".part", ".ytdl", ".tmp")


# --------------------------------------------------------------------------- #
#  Models and errors                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VideoInfo:
    platform: str
    title: str
    uploader: str
    duration: int
    view_count: int | None = None
    like_count: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    info: VideoInfo
    size_bytes: int

    def cleanup(self) -> None:
        shutil.rmtree(self.path.parent, ignore_errors=True)


class DownloadError(Exception):
    """The download failed: `text` is the HTML shown in chat, `reason` goes to logs."""

    # Worth an admin alert: something on our side needs fixing.
    alert = False

    def __init__(self, text: str, reason: str) -> None:
        super().__init__(reason)
        self.text = text
        self.reason = reason


class Rejected(DownloadError):
    """The link works, but it is not a short vertical video (photo, live, too long…)."""


class LoginRequired(DownloadError):
    """The site wants a logged-in session: cookies are missing or expired."""

    alert = True


class Blocked(DownloadError):
    """TikTok refused every impersonation target."""

    alert = True


# --------------------------------------------------------------------------- #
#  yt-dlp setup                                                               #
# --------------------------------------------------------------------------- #


class _YoutubeDL(yt_dlp.YoutubeDL):
    """yt-dlp that uses our impersonation targets when an extractor asks for "any"."""

    def __init__(self, params: dict, *, targets: tuple[str, ...] = ()) -> None:
        self._preferred_targets = targets
        super().__init__(params)

    def _parse_impersonate_targets(self, impersonate):
        wants_any = impersonate in (True, "") or impersonate == ImpersonateTarget()
        if self._preferred_targets and wants_any:
            impersonate = list(self._preferred_targets)
        return super()._parse_impersonate_targets(impersonate)


@functools.cache
def impersonate_targets() -> tuple[str, ...]:
    """Our preferred targets that this curl_cffi build supports (private yt-dlp API)."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "ignoreconfig": True}) as ydl:
            return tuple(
                target
                for target in IMPERSONATE_TARGETS
                if ydl._impersonate_target_available(ImpersonateTarget.from_str(target))
            )
    except Exception:
        log.debug("Could not probe impersonation targets.", exc_info=True)
        return ()


def log_impersonation_status() -> None:
    if targets := impersonate_targets():
        log.info("Impersonation ready. Targets=%s", ",".join(targets))
    else:
        log.error(
            "No usable impersonation target: TikTok links will fail. "
            "Check that curl-cffi is installed and supports one of: %s",
            ", ".join(IMPERSONATE_TARGETS),
        )


def format_selector(platform: str) -> str:
    """Formats to try, best first. Only vertical videos get here, so the width
    is the short side: `width<=1080` means up to 1080x1920."""
    short_side = "[width<=?1080]"
    size = f"{settings.max_file_size_mb}M"
    if platform == "tiktok":
        # Prefer a single H.264 file that already carries audio: TikTok's
        # separate video tracks are sometimes silent after merging.
        return (
            f"b[ext=mp4]{short_side}[vcodec=h264][acodec!=none][filesize<{size}]/"
            f"b[ext=mp4]{short_side}[vcodec=h264][acodec!=none][filesize_approx<{size}]/"
            f"b[ext=mp4]{short_side}[vcodec=h264][acodec!=none]/"
            f"b{short_side}[vcodec=h264][acodec!=none]/"
            f"bv[ext=mp4]{short_side}+ba[ext=m4a]/"
            f"bv{short_side}+ba/"
            f"b{short_side}/b"
        )

    # YouTube: the best H.264 that fits the limit (plays everywhere), then any
    # codec that fits, then 720p when sizes are unknown. Keep ~5 MB for audio.
    video = f"{max(1, settings.max_file_size_mb - 5)}M"
    return (
        f"bv*{short_side}[vcodec^=avc][filesize_approx<{video}]+ba[ext=m4a]/"
        f"bv*{short_side}[filesize_approx<{video}]+ba/"
        "bv*[width<=?720][vcodec^=avc]+ba[ext=m4a]/"
        f"b{short_side}/b"
    )


class _YtDlpLog:
    """yt-dlp prints errors to stderr even when quiet; we report them ourselves."""

    _log = logging.getLogger("yt_dlp")

    def debug(self, message: str) -> None:
        self._log.debug(message)

    info = warning = error = debug


def _options(
    platform: str,
    workdir: Path,
    cookie_file: str | None,
    on_progress: Callable[[float], None] | None,
) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtDlpLog(),
        "ignoreconfig": True,
        "noplaylist": True,
        # A profile or playlist link must not enumerate the whole channel.
        "extract_flat": "in_playlist",
        "socket_timeout": 30,
        "retries": RETRIES,
        "extractor_retries": RETRIES,
        "file_access_retries": RETRIES,
        "outtmpl": str(workdir / "video.%(ext)s"),
        "format": format_selector(platform),
        "merge_output_format": "mp4",
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    if on_progress:

        def hook(update: dict) -> None:
            total = update.get("total_bytes") or update.get("total_bytes_estimate")
            if update.get("status") == "downloading" and total:
                try:
                    on_progress(100 * (update.get("downloaded_bytes") or 0) / total)
                except Exception:  # never let a UI callback break the download
                    log.debug("Progress callback failed.", exc_info=True)

        opts["progress_hooks"] = [hook]
    return opts


# --------------------------------------------------------------------------- #
#  Metadata                                                                   #
# --------------------------------------------------------------------------- #


def parse_count(value: object) -> int | None:
    """Counters come as ints, floats or strings like "1.2K"."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)([KMB]?)", str(value).replace(",", "").replace(" ", ""), re.I
    )
    if not match:
        return None
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match[2].upper()]
    return int(float(match[1]) * multiplier)


def _first_text(meta: dict, *keys: str) -> str | None:
    for key in keys:
        if isinstance(value := meta.get(key), str) and value.strip():
            return value.strip()
    return None


def _first_count(meta: dict, *keys: str) -> int | None:
    for key in keys:
        if (count := parse_count(meta.get(key))) is not None:
            return count
    return None


def dimensions(meta: dict) -> tuple[int | None, int | None]:
    """Width and height; formats of one video share the orientation."""
    width, height = meta.get("width"), meta.get("height")
    if width and height:
        return int(width), int(height)
    for fmt in meta.get("formats") or []:
        if fmt.get("width") and fmt.get("height"):
            return int(fmt["width"]), int(fmt["height"])
    return None, None


def video_info(platform: str, meta: dict) -> VideoInfo:
    title = _first_text(meta, "title", "fulltitle", "alt_title") or "Без названия"
    uploader_keys = ("uploader", "channel", "creator", "artist", "uploader_id", "channel_id")
    if platform == "tiktok":
        # TikTok titles are often a generated "TikTok video #123"; the description is better.
        if re.fullmatch(r"TikTok video #\d+", title, re.I):
            title = _first_text(meta, "description", "fulltitle", "alt_title") or title
        uploader_keys = ("channel", "creator", "uploader", "uploader_id")
    width, height = dimensions(meta)
    return VideoInfo(
        platform=platform,
        title=title,
        uploader=_first_text(meta, *uploader_keys) or "Неизвестно",
        duration=int(meta.get("duration") or 0),
        view_count=_first_count(meta, "view_count", "play_count"),
        like_count=_first_count(meta, "like_count"),
        width=width,
        height=height,
    )


def validate(platform: str, meta: dict) -> VideoInfo:
    """Only short vertical videos pass; everything else raises `Rejected`."""
    if meta.get("_type") == "playlist" or meta.get("entries") is not None:
        raise Rejected(texts.PROFILE, "profile or playlist")
    if meta.get("is_live") or meta.get("live_status") in _LIVE_STATUSES:
        raise Rejected(texts.LIVE, "live stream")
    formats = meta.get("formats") or []
    if formats and all(fmt.get("vcodec") in (None, "none") for fmt in formats):
        raise Rejected(texts.PHOTO, "photo post")

    info = video_info(platform, meta)
    # Orientation first: a long horizontal video is rejected for being
    # horizontal, which explains the actual problem better than its length.
    if info.width and info.height and info.height <= info.width:
        raise Rejected(texts.HORIZONTAL, f"horizontal {info.width}x{info.height}")
    if info.duration > settings.max_duration_sec:
        raise Rejected(
            texts.too_long(info.duration, settings.max_duration_sec),
            f"too long {info.duration}s",
        )
    return info


# --------------------------------------------------------------------------- #
#  Download                                                                   #
# --------------------------------------------------------------------------- #


def _has(message: str, tokens: tuple[str, ...]) -> bool:
    lowered = message.lower()
    return any(token in lowered for token in tokens)


def clean_error(message: str) -> str:
    """`ERROR: [TikTok] 7234: Video unavailable` → `Video unavailable`."""
    message = re.sub(r"^ERROR:\s*", "", message.strip())
    message = re.sub(r"^\[[\w:]+\]\s*[\w-]*:\s*", "", message)
    return message[:200]


def user_error(message: str, url: str) -> DownloadError:
    """Map a yt-dlp error to the error shown in chat."""
    reason = clean_error(message)
    if _has(message, _BLOCKED_ERRORS):
        return Blocked(texts.BLOCKED, reason)
    if _has(message, _LOGIN_ERRORS):
        return LoginRequired(texts.LOGIN, reason)
    if _has(message, _NETWORK_ERRORS):
        return DownloadError(texts.NETWORK, reason)
    if "unsupported url" in message.lower() and "/photo/" in (url + message).lower():
        return Rejected(texts.PHOTO, "photo post")
    return DownloadError(texts.failed(reason), reason)


def _extract(url: str, opts: dict) -> tuple[yt_dlp.YoutubeDL, dict]:
    """Extract metadata, rotating blocked impersonation targets and retrying network errors.

    Returns the open YoutubeDL that succeeded, so the download reuses its
    session (TikTok ties the video URL to cookies set by the page request).
    """
    targets = impersonate_targets()
    attempt = 0
    while True:
        ydl = _YoutubeDL(opts, targets=targets)
        try:
            return ydl, ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as error:
            ydl.close()
            message = str(error)
            if _has(message, _BLOCKED_ERRORS) and len(targets) > 1:
                log.warning("Impersonation blocked. Target=%s Next=%s", targets[0], targets[1])
                targets = targets[1:]
                continue
            attempt += 1
            if attempt >= RETRIES or not _has(message, _NETWORK_ERRORS):
                raise user_error(message, url) from error
            delay = min(2 ** (attempt - 1), RETRY_MAX_SLEEP_SEC)
            log.warning(
                "Network error, retrying. Attempt=%d/%d Delay=%.0fs Error=%s",
                attempt,
                RETRIES,
                delay,
                clean_error(message),
            )
            time.sleep(delay)


def has_audio(path: Path) -> bool | None:
    """Whether the file has an audio stream; None when ffprobe cannot tell."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", path],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        log.warning("Could not probe audio. File=%s Error=%s", path.name, error)
        return None
    return any(stream.get("codec_type") == "audio" for stream in streams)


def _output_file(workdir: Path) -> Path:
    files = [p for p in workdir.iterdir() if p.is_file() and p.suffix not in _TEMP_SUFFIXES]
    if not files:
        raise DownloadError(texts.NOT_FOUND, "no output file")
    return max(files, key=lambda p: p.stat().st_size)


def download_video(
    url: str,
    *,
    on_info: Callable[[VideoInfo], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> DownloadResult:
    """Download a short vertical video into its own temp directory.

    Blocking: call it from a worker thread. `on_info` fires once the video has
    passed validation, `on_progress` with a 0-100 percentage while it
    downloads. The caller owns the result and must call `cleanup()`.
    """
    platform = platform_of(url)
    started = time.monotonic()
    workdir = Path(tempfile.mkdtemp(prefix="sbahelper-"))
    try:
        with cookies.session(settings.cookies_dir, platform) as cookie_file:
            opts = _options(platform, workdir, cookie_file, on_progress)
            ydl, meta = _extract(url, opts)
            with ydl:
                info = validate(platform, meta)
                if on_info:
                    on_info(info)
                try:
                    ydl.process_ie_result(meta, download=True)
                except yt_dlp.utils.DownloadError as error:
                    raise user_error(str(error), url) from error

        path = _output_file(workdir)
        if platform == "tiktok" and has_audio(path) is False:
            raise DownloadError(texts.NO_AUDIO, "no audio track")
        size = path.stat().st_size
        if size > settings.max_file_size_mb * 1024**2:
            raise Rejected(
                texts.too_big(size / 1024**2, settings.max_file_size_mb),
                f"too big {size / 1024**2:.1f}MB",
            )
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    log.info(
        "Downloaded. Platform=%s Size=%.1fMB Duration=%ds Time=%.1fs",
        platform,
        size / 1024**2,
        info.duration,
        time.monotonic() - started,
    )
    return DownloadResult(path, info, size)
