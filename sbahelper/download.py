"""yt-dlp engine: extract once, validate, then download from that same info.

One extraction per link keeps TikTok requests (and their bot detection) to a
minimum. Everything platform-specific lives here: format selectors, browser
impersonation targets, metadata cleanup and the rules that keep the bot to
short vertical videos. Errors carry a `code` that `texts` turns into a
localised message, and a sanitised `reason` for logs and /check.

Blocking: run `download_video` in a worker thread. It cannot be killed from
outside, so it cooperates: a `Cancel` token is checked between steps, inside
yt-dlp's progress hook (raising `DownloadCancelled` stops the transfer) and
during retry back-off. Subprocesses (ffprobe/ffmpeg) have their own timeouts.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DownloadCancelled

from sbahelper import cookies, media
from sbahelper.config import settings
from sbahelper.links import platform_of, video_key
from sbahelper.redact import redact

log = logging.getLogger(__name__)

# Quick in-process retries for flaky networks. There is no delayed retry: a
# link that fails here is reported and forgotten.
RETRIES = 3
RETRY_MAX_SLEEP_SEC = 8.0
# Bumped whenever selection or validation rules change in a way that should
# not reuse files cached under the old rules.
MEDIA_POLICY_VERSION = 2
# Work directories are recognisable by this prefix and marker file, so startup
# cleanup only ever removes what this application created.
WORKDIR_PREFIX = "sbahelper-dl-"
WORKDIR_MARKER = ".sbahelper-workdir"
# Room for the separate video and audio tracks plus the merged result.
DISK_HEADROOM_FACTOR = 3
# A metadata size estimate this much above the limit is rejected before downloading.
ESTIMATE_TOLERANCE = 1.10
# Extractors yt-dlp may use; the generic extractor is never allowed.
ALLOWED_EXTRACTORS = ("youtube(?::.*)?$", "tiktok(?::.*)?$", r"vm\.tiktok$")

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
_PRIVATE_ERRORS = (
    "private video",
    "this video is private",
    "this account is private",
    "members-only",
    "join this channel",
)
_UNAVAILABLE_ERRORS = (
    "video unavailable",
    "this video is unavailable",
    "this video has been removed",
    "video has been removed",
    "this video is no longer available",
    "has been terminated",
    "http error 404",
    "post not found",
    "status code 10204",
    "status code 10216",
    "video not available",
    "not available in your country",
)
_TOOL_ERRORS = ("ffmpeg is not installed", "ffprobe and ffmpeg not found", "ffmpeg not found")
_UNSUPPORTED_ERRORS = ("unsupported url", "no suitable extractor")
_LIVE_STATUSES = ("is_live", "is_upcoming", "was_live", "post_live")
_TEMP_SUFFIXES = (".part", ".ytdl", ".tmp", ".temp")


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
    # The site's own id (yt-dlp's `id`), when known.
    video_id: str | None = None

    @property
    def key(self) -> str | None:
        return video_key(self.platform, self.video_id)


@dataclass(slots=True)
class DownloadResult:
    path: Path
    info: VideoInfo
    size_bytes: int
    # The work directory; owned by whoever holds the result until `cleanup()`.
    workdir: Path | None = None

    def cleanup(self) -> None:
        remove_workdir(self.workdir or self.path.parent)


class DownloadError(Exception):
    """The download failed. `code` selects the chat message, `reason` goes to logs.

    `params` fill the message (sizes, durations). `alert`: something on our
    side needs an admin (logged at ERROR, which DMs the admins).
    """

    code = "failed"
    alert = False

    def __init__(self, reason: str, *, code: str | None = None, **params: object) -> None:
        reason = redact(reason)
        super().__init__(reason)
        self.reason = reason
        if code:
            self.code = code
        self.params = params


class Rejected(DownloadError):
    """The link works, but it is not a short vertical video (photo, live, too long…)."""

    code = "rejected"


class Unavailable(DownloadError):
    """The site says the video does not exist (any more) or is not shown here."""

    code = "unavailable"


class PrivateVideo(DownloadError):
    """Private or members-only content."""

    code = "private"


class LoginRequired(DownloadError):
    """The site wants a logged-in session. Missing or rejected cookies are likely,
    though the site may also be asking for other reasons."""

    code = "login"
    alert = True


class Blocked(DownloadError):
    """TikTok refused every impersonation target."""

    code = "blocked"
    alert = True


class NetworkFailure(DownloadError):
    code = "network"


class ToolMissing(DownloadError):
    """ffmpeg/ffprobe is missing or broken: every download will fail."""

    code = "tools"
    alert = True


class MediaError(DownloadError):
    """The file arrived incomplete or is not playable video."""

    code = "media"


class TimedOut(DownloadError):
    code = "timeout"


class Cancelled(DownloadError):
    """Stopped on purpose (shutdown)."""

    code = "cancelled"


class Satisfied(Exception):
    """Raised by `on_info` to stop before downloading (e.g. a cache hit)."""

    def __init__(self, value: object = None) -> None:
        super().__init__("satisfied")
        self.value = value


class _Abort(DownloadCancelled):
    """Carries our error through yt-dlp, which stops on `DownloadCancelled`."""

    def __init__(self, error: DownloadError) -> None:
        super().__init__(error.reason)
        self.error = error


@dataclass(slots=True)
class Cancel:
    """Cooperative cancellation and deadline for one worker."""

    deadline: float | None = None
    _event: threading.Event = field(default_factory=threading.Event)
    reason: str = ""

    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled(self.reason or "cancelled")
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise TimedOut("deadline exceeded")

    def sleep(self, seconds: float) -> None:
        """Back-off that wakes up early on cancellation."""
        if self.deadline is not None:
            seconds = min(seconds, max(0.0, self.deadline - time.monotonic()))
        self._event.wait(seconds)
        self.check()


# --------------------------------------------------------------------------- #
#  Work directories                                                           #
# --------------------------------------------------------------------------- #


def make_workdir(root: Path | None = None) -> Path:
    root = root or settings.temp_dir
    root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=root))
    (workdir / WORKDIR_MARKER).write_text(f"{os.getpid()}\n", encoding="ascii")
    return workdir


def remove_workdir(path: Path) -> None:
    if path.name.startswith(WORKDIR_PREFIX) and (path / WORKDIR_MARKER).exists():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        log.warning("Not removing an unknown directory. Path=%s", path)


def cleanup_stale_workdirs(root: Path | None = None, older_than_sec: float = 0) -> int:
    """Remove work directories left behind by a previous run (crash, kill -9).

    Only directories with our prefix *and* our marker file are touched. Call
    it before any worker starts, or with `older_than_sec` above the download
    timeout, so a live worker's directory is never removed.
    """
    root = root or settings.temp_dir
    removed = 0
    try:
        candidates = list(root.glob(f"{WORKDIR_PREFIX}*"))
    except OSError:
        return 0
    now = time.time()
    for path in candidates:
        try:
            marker = path / WORKDIR_MARKER
            if not path.is_dir() or path.is_symlink() or not marker.is_file():
                continue
            if now - marker.stat().st_mtime < older_than_sec:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def free_bytes(root: Path | None = None) -> int | None:
    try:
        return shutil.disk_usage(root or settings.temp_dir).free
    except OSError:
        return None


def required_free_bytes() -> int:
    return settings.max_file_size_bytes * DISK_HEADROOM_FACTOR


# --------------------------------------------------------------------------- #
#  yt-dlp setup                                                               #
# --------------------------------------------------------------------------- #


class _YoutubeDL(yt_dlp.YoutubeDL):
    """yt-dlp that uses our impersonation targets when an extractor asks for "any".

    Overrides the private `_parse_impersonate_targets`; tests/test_ytdlp_compat.py
    checks it against the installed yt-dlp.
    """

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


def format_selector(platform: str, max_bytes: int | None = None) -> str:
    """Formats to try, best first.

    Sizes are plain byte counts: yt-dlp reads `50M` as 50*10^6 and `50MiB` as
    50*2^20, so a unit would be one more thing to get wrong. Only vertical
    videos get here, so the width is the short side: `width<=1080` means up
    to 1080x1920.
    """
    limit = max_bytes or settings.max_file_size_bytes
    short_side = "[width<=?1080]"
    if platform == "tiktok":
        # Prefer a single H.264 file that already carries audio: TikTok's
        # separate video tracks are sometimes silent after merging.
        return (
            f"b[ext=mp4]{short_side}[vcodec=h264][acodec!=none][filesize<{limit}]/"
            f"b[ext=mp4]{short_side}[vcodec=h264][acodec!=none][filesize_approx<{limit}]/"
            f"b[ext=mp4]{short_side}[vcodec=h264][acodec!=none]/"
            f"b{short_side}[vcodec=h264][acodec!=none]/"
            f"bv[ext=mp4]{short_side}+ba[ext=m4a]/"
            f"bv{short_side}+ba/"
            f"b{short_side}/b"
        )

    # YouTube: the best H.264 that fits the limit (plays everywhere), then any
    # codec that fits, then 720p when sizes are unknown. Keep ~5 MB for audio.
    video = max(1_000_000, limit - 5_000_000)
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
    cancel: Cancel | None = None,
) -> dict:
    max_bytes = settings.max_file_size_bytes
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtDlpLog(),
        "ignoreconfig": True,
        "noplaylist": True,
        # A profile or playlist link must not enumerate the whole channel.
        "extract_flat": "in_playlist",
        "allowed_extractors": list(ALLOWED_EXTRACTORS),
        "socket_timeout": 30,
        "retries": RETRIES,
        "extractor_retries": RETRIES,
        "file_access_retries": RETRIES,
        "outtmpl": str(workdir / "video.%(ext)s"),
        "paths": {"temp": str(workdir)},
        "format": format_selector(platform, max_bytes),
        "merge_output_format": "mp4",
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file

    def hook(update: dict) -> None:
        if cancel is not None:
            try:
                cancel.check()
            except DownloadError as error:
                raise _Abort(error) from None
        downloaded = update.get("downloaded_bytes") or 0
        total = update.get("total_bytes") or update.get("total_bytes_estimate")
        if downloaded > max_bytes or (update.get("total_bytes") or 0) > max_bytes:
            size = max(downloaded, update.get("total_bytes") or 0)
            raise _Abort(Rejected(f"too big {size} bytes", code="too_big", size=size))
        if on_progress and update.get("status") == "downloading" and total:
            try:
                on_progress(100 * downloaded / total)
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
    # Placeholders are filled in by `texts` in the reader's language.
    title = _first_text(meta, "title", "fulltitle", "alt_title") or ""
    uploader_keys = ("uploader", "channel", "creator", "artist", "uploader_id", "channel_id")
    if platform == "tiktok":
        # TikTok titles are often a generated "TikTok video #123"; the description is better.
        if not title or re.fullmatch(r"TikTok video #\d+", title, re.I):
            title = _first_text(meta, "description", "fulltitle", "alt_title") or title
        uploader_keys = ("channel", "creator", "uploader", "uploader_id")
    width, height = dimensions(meta)
    video_id = meta.get("id")
    return VideoInfo(
        platform=platform,
        title=title,
        uploader=_first_text(meta, *uploader_keys) or "",
        duration=int(meta.get("duration") or 0),
        view_count=_first_count(meta, "view_count", "play_count"),
        like_count=_first_count(meta, "like_count"),
        width=width,
        height=height,
        video_id=str(video_id) if video_id else None,
    )


def estimated_size(meta: dict) -> int | None:
    """Bytes of the chosen format(s) according to the site, if every part is known."""
    parts = meta.get("requested_formats") or [meta]
    total = 0
    for part in parts:
        size = part.get("filesize") or part.get("filesize_approx")
        if not size:
            return None
        total += int(size)
    return total


def validate(platform: str, meta: dict, *, url: str = "") -> VideoInfo:
    """Only short vertical videos pass; everything else raises `Rejected`."""
    if meta.get("_type") == "playlist" or meta.get("entries") is not None:
        raise Rejected("profile or playlist", code="profile")
    page = meta.get("webpage_url") or url
    if page and platform_of(page) != platform:
        raise Rejected(f"resolved to another site: {platform_of(page)}", code="unsupported")
    if meta.get("is_live") or meta.get("live_status") in _LIVE_STATUSES:
        raise Rejected("live stream", code="live")
    formats = meta.get("formats") or []
    if formats and all(fmt.get("vcodec") in (None, "none") for fmt in formats):
        raise Rejected("photo post", code="photo")

    info = video_info(platform, meta)
    # Orientation first: a long horizontal video is rejected for being
    # horizontal, which explains the actual problem better than its length.
    if info.width and info.height and info.height <= info.width:
        raise Rejected(f"horizontal {info.width}x{info.height}", code="horizontal")
    if info.duration > settings.max_duration_sec:
        raise Rejected(
            f"too long {info.duration}s",
            code="too_long",
            duration=info.duration,
            limit=settings.max_duration_sec,
        )
    estimate = estimated_size(meta)
    if estimate and estimate > settings.max_file_size_bytes * ESTIMATE_TOLERANCE:
        raise Rejected(f"too big (estimated {estimate} bytes)", code="too_big", size=estimate)
    return info


# --------------------------------------------------------------------------- #
#  Errors                                                                     #
# --------------------------------------------------------------------------- #


def _has(message: str, tokens: tuple[str, ...]) -> bool:
    lowered = message.lower()
    return any(token in lowered for token in tokens)


def clean_error(message: str) -> str:
    """`ERROR: [TikTok] 7234: Video unavailable` → `Video unavailable` (redacted, short)."""
    message = re.sub(r"^ERROR:\s*", "", message.strip())
    message = re.sub(r"^\[[\w:.]+\]\s*[\w-]*:\s*", "", message)
    return redact(message)[:200]


def user_error(message: str, url: str) -> DownloadError:
    """Map a yt-dlp error to one of our categories."""
    reason = clean_error(message)
    if _has(message, _BLOCKED_ERRORS):
        return Blocked(reason)
    if _has(message, _TOOL_ERRORS):
        return ToolMissing(reason)
    if _has(message, _PRIVATE_ERRORS):
        return PrivateVideo(reason)
    if _has(message, _LOGIN_ERRORS):
        return LoginRequired(reason)
    if _has(message, _UNSUPPORTED_ERRORS):
        if "/photo/" in (url + message).lower():
            return Rejected("photo post", code="photo")
        return Rejected(reason, code="unsupported")
    if _has(message, _UNAVAILABLE_ERRORS):
        return Unavailable(reason)
    if _has(message, _NETWORK_ERRORS):
        return NetworkFailure(reason)
    return DownloadError(reason)


# --------------------------------------------------------------------------- #
#  Extraction                                                                 #
# --------------------------------------------------------------------------- #


def _extract(url: str, opts: dict, cancel: Cancel | None = None) -> tuple[yt_dlp.YoutubeDL, dict]:
    """Extract metadata, rotating blocked impersonation targets and retrying network errors.

    Returns the open YoutubeDL that succeeded, so the download reuses its
    session (TikTok ties the video URL to cookies set by the page request).
    """
    cancel = cancel or Cancel()
    targets = impersonate_targets()
    attempt = 0
    while True:
        cancel.check()
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
            cancel.sleep(delay)
        except BaseException:
            ydl.close()
            raise


def _extract_recorded(
    url: str,
    opts: dict,
    platform: str,
    session: cookies.CookieSession | None,
    cancel: Cancel | None = None,
) -> tuple[yt_dlp.YoutubeDL, dict]:
    """`_extract`, remembering whether the site accepted our cookies."""
    generation = session.generation if session else None
    try:
        ydl, meta = _extract(url, opts, cancel)
    except LoginRequired as error:
        if session:
            cookies.record(platform, False, error.reason, generation=generation)
        raise
    if session:
        cookies.record(platform, True, generation=generation)
    return ydl, meta


def refresh(url: str) -> None:
    """Open one video's page with the platform cookies, without downloading it.

    Sites rotate session cookies on visits, so a session nobody uses goes
    stale sooner. The rotated cookies are saved by `cookies.session`. This
    helps; it does not guarantee the site keeps accepting the session.
    Raises `DownloadError` like `download_video`.
    """
    platform = platform_of(url)
    workdir = make_workdir()
    try:
        with cookies.session(settings.cookies_dir, platform) as session:
            if session is None:
                return
            opts = _options(platform, workdir, session.path, None)
            ydl, _ = _extract_recorded(url, opts, platform, session)
            ydl.close()
    finally:
        remove_workdir(workdir)


# --------------------------------------------------------------------------- #
#  Download                                                                   #
# --------------------------------------------------------------------------- #


def has_audio(path: Path) -> bool | None:
    """Whether the file has an audio stream; None when ffprobe cannot tell."""
    try:
        return media.probe(path).has_audio
    except (media.ToolMissing, media.ProbeFailed) as error:
        log.warning("Could not probe audio. File=%s Error=%s", path.name, error)
        return None


def _output_file(workdir: Path) -> Path:
    files = [
        p
        for p in workdir.iterdir()
        if p.is_file()
        and p.name != WORKDIR_MARKER
        and p.suffix not in _TEMP_SUFFIXES
        and ".part-Frag" not in p.name
    ]
    if not files:
        raise MediaError("no output file")
    return max(files, key=lambda p: p.stat().st_size)


def check_media(path: Path, info: VideoInfo) -> tuple[Path, VideoInfo]:
    """Validate the file itself and return the file to send with its real size.

    Raises `MediaError` for incomplete or unplayable files, `Rejected` when the
    real file breaks a rule the metadata did not show, `ToolMissing` without
    ffprobe.
    """
    if any(p.suffix in _TEMP_SUFFIXES or ".part-Frag" in p.name for p in path.parent.iterdir()):
        raise MediaError("download left partial fragments")
    try:
        probed = media.probe(path)
    except media.ToolMissing as error:
        raise ToolMissing(str(error)) from error
    except media.ProbeFailed as error:
        raise MediaError(f"unreadable media: {error}") from error
    if not probed.has_video:
        raise MediaError("no video stream")
    if info.platform == "tiktok" and not probed.has_audio:
        raise MediaError("no audio track", code="no_audio")
    if probed.width and probed.height and probed.height <= probed.width:
        raise Rejected(f"horizontal {probed.width}x{probed.height}", code="horizontal")
    duration = round(probed.duration)
    if duration > settings.max_duration_sec + 2:
        raise Rejected(
            f"too long {duration}s",
            code="too_long",
            duration=duration,
            limit=settings.max_duration_sec,
        )
    if info.duration and probed.duration < info.duration * 0.8 - 2:
        raise MediaError(f"incomplete: {probed.duration:.1f}s of {info.duration}s")
    try:
        path = media.make_streamable(path, probed)
    except media.ToolMissing as error:
        raise ToolMissing(str(error)) from error
    return path, replace(
        info,
        duration=duration or info.duration,
        width=probed.width or info.width,
        height=probed.height or info.height,
    )


def download_video(
    url: str,
    *,
    on_info: Callable[[VideoInfo], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancel: Cancel | None = None,
) -> DownloadResult:
    """Download a short vertical video into its own work directory.

    Blocking: call it from a worker thread. `on_info` fires once the video has
    passed validation and may raise `Satisfied` to stop there. `on_progress`
    gets a 0-100 percentage while the file downloads. The caller owns the
    result and must call `cleanup()`.
    """
    cancel = cancel or Cancel()
    platform = platform_of(url)
    started = time.monotonic()
    workdir = make_workdir()
    try:
        with cookies.session(settings.cookies_dir, platform) as session:
            opts = _options(
                platform, workdir, session.path if session else None, on_progress, cancel
            )
            ydl, meta = _extract_recorded(url, opts, platform, session, cancel)
            with ydl:
                info = validate(platform, meta, url=url)
                if on_info:
                    on_info(info)
                cancel.check()
                try:
                    ydl.process_ie_result(meta, download=True)
                except _Abort as abort:
                    raise abort.error from None
                except yt_dlp.utils.DownloadError as error:
                    raise user_error(str(error), url) from error

        cancel.check()
        path, info = check_media(_output_file(workdir), info)
        size = path.stat().st_size
        if size > settings.max_file_size_bytes:
            raise Rejected(f"too big {size} bytes", code="too_big", size=size)
    except BaseException:
        remove_workdir(workdir)
        raise

    log.info(
        "Downloaded. Platform=%s Video=%s Size=%d Duration=%ds Time=%.1fs",
        platform,
        info.key,
        size,
        info.duration,
        time.monotonic() - started,
    )
    return DownloadResult(path, info, size, workdir)
