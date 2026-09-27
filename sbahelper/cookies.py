"""Cookies for yt-dlp: one Netscape cookie file per platform in COOKIES_DIR.

Two ways to add cookies, neither needs a restart or an env var:

- an admin sends a cookie file to the bot in a private chat;
- any other `*.txt` / `*.json` put into COOKIES_DIR is imported on startup
  (and renamed to `*.imported`), e.g. the old `cookies.txt`.

Both accept a Netscape `cookies.txt` or a JSON export (Cookie-Editor,
EditThisCookie), split the cookies by domain and replace `tiktok.txt` /
`youtube.txt`. One file with every site in it is fine.

Each download works on a private copy of the platform file (`session()`).
Sites rotate session cookies on almost every request (YouTube's
`__Secure-*PSIDTS` go stale within hours), so whatever a run changed is merged
back into the platform file, after failed runs too, and parallel runs merge
their own changes instead of overwriting each other's.

`record()` keeps the last real answer from the site per platform ("accepted"
or "asks to log in"), because an expiry date in the file says nothing about
whether the site still accepts the session.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Cookie domains that belong to each platform's file.
PLATFORM_DOMAINS = {
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "google.com"),
}
# Cookies that mean "logged in". Their expiry is the one that matters.
LOGIN_COOKIES = {
    "tiktok": ("sessionid", "sessionid_ss", "sid_tt"),
    "youtube": ("LOGIN_INFO", "__Secure-3PSID", "SID"),
}
IMPORTABLE_SUFFIXES = (".txt", ".json")
MAX_FILE_BYTES = 1024 * 1024
_HEADER = "# Netscape HTTP Cookie File\n# Written by sbahelper. yt-dlp keeps it up to date.\n\n"

# Guards the platform files and the state below across download threads.
_lock = threading.Lock()
# Bumped by every import: a run that started on older cookies must not merge
# its changes into a freshly uploaded session, and old health no longer applies.
_generation: dict[str, int] = {}
_health: dict[str, tuple[int, Health]] = {}


class CookieError(ValueError):
    """The file is not a cookie export we can read."""


@dataclass(frozen=True, slots=True)
class Cookie:
    domain: str
    name: str
    value: str
    path: str = "/"
    secure: bool = False
    # Unix seconds; 0 is a session cookie.
    expires: int = 0
    http_only: bool = False

    def belongs_to(self, domains: tuple[str, ...]) -> bool:
        host = self.domain.lstrip(".").lower()
        return any(host == domain or host.endswith(f".{domain}") for domain in domains)

    def to_line(self) -> str:
        return "\t".join(
            (
                ("#HttpOnly_" if self.http_only else "") + self.domain,
                _flag(self.domain.startswith(".")),
                self.path,
                _flag(self.secure),
                str(self.expires),
                self.name,
                self.value,
            )
        )


@dataclass(frozen=True, slots=True)
class Health:
    """The last time the site saw these cookies: accepted them or asked to log in."""

    ok: bool
    at: datetime
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CookieStatus:
    platform: str
    count: int
    logged_in: bool
    # Earliest expiry among the login cookies; None for session-only or no login.
    login_expires: datetime | None
    updated: datetime | None
    # None until a download or check has used the current cookies.
    health: Health | None = None


def _flag(value: bool) -> str:
    return "TRUE" if value else "FALSE"


# --------------------------------------------------------------------------- #
#  Parsing                                                                    #
# --------------------------------------------------------------------------- #


def parse(text: str) -> list[Cookie]:
    """Cookies from a Netscape cookies.txt or a JSON export."""
    stripped = text.lstrip("\ufeff \t\r\n")
    is_json = stripped.startswith(("[", "{"))
    cookies = _parse_json(stripped) if is_json else _parse_netscape(stripped)
    if not cookies:
        raise CookieError("no cookies in the file")
    return cookies


def _parse_netscape(text: str) -> list[Cookie]:
    cookies = []
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.rstrip("\r\n")
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line.removeprefix("#HttpOnly_")
        elif not line.strip() or line.startswith("#"):
            continue

        fields = line.split("\t")
        if len(fields) == 6:  # empty value with the trailing tab trimmed
            fields.append("")
        if len(fields) != 7:
            raise CookieError(f"line {number} does not have 7 tab-separated fields")
        domain, _, path, secure, expires, name, value = fields
        cookies.append(
            Cookie(
                domain=domain,
                name=name,
                value=value,
                path=path or "/",
                secure=secure.upper() == "TRUE",
                expires=_seconds(expires, number),
                http_only=http_only,
            )
        )
    return cookies


def _parse_json(text: str) -> list[Cookie]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise CookieError(f"invalid JSON ({error.msg}, line {error.lineno})") from error
    if isinstance(data, dict):  # {"url": ..., "cookies": [...]} style exports
        data = data.get("cookies")
    if not isinstance(data, list):
        raise CookieError("expected a JSON list of cookies")

    cookies = []
    for number, item in enumerate(data, 1):
        if not isinstance(item, dict) or not item.get("name") or not item.get("domain"):
            raise CookieError(f"cookie #{number} has no name or domain")
        domain = str(item["domain"])
        if not item.get("hostOnly") and not domain.startswith("."):
            domain = f".{domain}"
        expires = 0 if item.get("session") else item.get("expirationDate", item.get("expires"))
        cookies.append(
            Cookie(
                domain=domain,
                name=str(item["name"]),
                value=str(item.get("value", "")),
                path=str(item.get("path") or "/"),
                secure=bool(item.get("secure")),
                expires=_seconds(expires, number),
                http_only=bool(item.get("httpOnly")),
            )
        )
    return cookies


def _seconds(raw: object, where: int) -> int:
    if raw in (None, ""):
        return 0
    try:
        return max(0, int(float(str(raw))))
    except ValueError:
        raise CookieError(f"bad expiry {raw!r} (entry {where})") from None


# --------------------------------------------------------------------------- #
#  Files                                                                      #
# --------------------------------------------------------------------------- #


def platform_file(directory: Path, platform: str) -> Path:
    return directory / f"{platform}.txt"


def import_text(directory: Path, text: str, *, source: str = "upload") -> dict[str, int]:
    """Split an export by platform and replace each platform's file.

    Returns how many cookies each platform got. Raises `CookieError` when the
    text is unreadable or holds no TikTok/YouTube cookies at all.
    """
    cookies = parse(text)
    groups = {
        platform: [cookie for cookie in cookies if cookie.belongs_to(domains)]
        for platform, domains in PLATFORM_DOMAINS.items()
    }
    groups = {platform: items for platform, items in groups.items() if items}
    if not groups:
        raise CookieError("no TikTok or YouTube cookies in the file")

    directory.mkdir(parents=True, exist_ok=True)
    for platform, items in groups.items():
        with _lock:
            _write_atomic(platform_file(directory, platform), items)
            _generation[platform] = _generation.get(platform, 0) + 1
        log.info("Cookies imported. Platform=%s Count=%d Source=%s", platform, len(items), source)
    return {platform: len(items) for platform, items in groups.items()}


def import_dropped(directory: Path) -> None:
    """Import every stray cookie export in `directory`, then rename it to `*.imported`."""
    if not directory.is_dir():
        return
    platform_files = {platform_file(directory, p).name for p in PLATFORM_DOMAINS}
    for path in sorted(directory.iterdir()):
        if (
            not path.is_file()
            or path.name in platform_files
            or path.suffix.lower() not in IMPORTABLE_SUFFIXES
        ):
            continue
        try:
            import_text(directory, path.read_text(encoding="utf-8-sig"), source=path.name)
        except (CookieError, OSError, UnicodeDecodeError) as error:
            log.warning("Cookie file skipped. File=%s Error=%s", path.name, error)
            continue
        with suppress(OSError):
            path.rename(path.with_name(f"{path.name}.imported"))


def _write_atomic(path: Path, cookies: list[Cookie]) -> None:
    body = _HEADER + "\n".join(cookie.to_line() for cookie in cookies) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp)
        raise


@contextmanager
def session(directory: Path, platform: str) -> Iterator[str | None]:
    """A private copy of the platform's cookie file for one yt-dlp run.

    yt-dlp rewrites its cookie file when it closes, failed runs included. The
    cookies that run added, changed or dropped are merged into the platform
    file under a lock, so parallel runs keep each other's rotations. Nothing
    is merged if new cookies were imported meanwhile.
    """
    source = platform_file(directory, platform)
    with _lock:
        if not source.is_file():
            tmp = None
        else:
            tmp, writable = _copy(source, directory, platform)
            generation = _generation.get(platform, 0)
            try:
                start = _read(Path(tmp))
            except OSError, CookieError, UnicodeDecodeError:
                start = {}  # unreadable: yt-dlp will say so; nothing to merge into
    if tmp is None:
        yield None
        return

    try:
        yield tmp
    finally:
        if writable:
            _merge_back(source, platform, generation, start, Path(tmp))
        with suppress(OSError):
            os.remove(tmp)


def _copy(source: Path, directory: Path, platform: str) -> tuple[str, bool]:
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{platform}-", suffix=".tmp")
        writable = True
    except OSError:  # read-only cookie directory: use a throwaway copy
        fd, tmp = tempfile.mkstemp(prefix=f"sbahelper-{platform}-", suffix=".txt")
        writable = False
    os.close(fd)
    try:
        shutil.copyfile(source, tmp)
    except BaseException:
        with suppress(OSError):
            os.remove(tmp)
        raise
    return tmp, writable


type _Jar = dict[tuple[str, str, str], Cookie]


def _read(path: Path) -> _Jar:
    return {(c.domain, c.path, c.name): c for c in _parse_netscape(path.read_text("utf-8"))}


def _merge_back(source: Path, platform: str, generation: int, start: _Jar, copy: Path) -> None:
    try:
        end = _read(copy)
    except OSError, CookieError, UnicodeDecodeError:
        log.debug("Cookie copy not merged. Platform=%s", platform, exc_info=True)
        return
    with _lock:
        if _generation.get(platform, 0) != generation:
            return  # new cookies were imported during the run
        try:
            current = _read(source)
        except OSError, CookieError, UnicodeDecodeError:
            return
        merged = dict(current)
        for key, cookie in end.items():
            before = start.get(key)
            if before is None or (before.value, before.expires) != (cookie.value, cookie.expires):
                # yt-dlp does not write the HttpOnly marker: keep ours.
                old = current.get(key) or before
                merged[key] = replace(cookie, http_only=old.http_only) if old else cookie
        for key in start.keys() - end.keys():
            merged.pop(key, None)
        if merged == current:
            return
        try:
            _write_atomic(source, list(merged.values()))
        except OSError as error:
            log.warning("Refreshed cookies not saved. Platform=%s Error=%s", platform, error)


def record(platform: str, ok: bool, reason: str = "") -> None:
    """Remember whether the site accepted the current cookies."""
    with _lock:
        _health[platform] = (_generation.get(platform, 0), Health(ok, datetime.now(UTC), reason))


def health(platform: str) -> Health | None:
    """The last recorded result for the current cookies; None after a new import."""
    with _lock:
        generation, result = _health.get(platform, (-1, None))
        return result if generation == _generation.get(platform, 0) else None


def status(directory: Path, now: datetime | None = None) -> list[CookieStatus]:
    now_ts = (now or datetime.now(UTC)).timestamp()
    statuses = []
    for platform in PLATFORM_DOMAINS:
        path = platform_file(directory, platform)
        try:
            cookies = _parse_netscape(path.read_text(encoding="utf-8"))
            updated = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError, CookieError, UnicodeDecodeError:
            statuses.append(CookieStatus(platform, 0, False, None, None))
            continue

        login = [c for c in cookies if c.name in LOGIN_COOKIES[platform]]
        alive = [c for c in login if not c.expires or c.expires > now_ts]
        expiries = [c.expires for c in (alive or login) if c.expires]
        statuses.append(
            CookieStatus(
                platform=platform,
                count=len(cookies),
                logged_in=bool(alive),
                login_expires=datetime.fromtimestamp(min(expiries), UTC) if expiries else None,
                updated=updated,
                health=health(platform),
            )
        )
    return statuses
