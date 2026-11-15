"""Scrub credentials out of text that leaves the process (logs, tracebacks, alerts).

Applied to whole formatted log lines, tracebacks included, so it also covers
messages written by libraries (httpx logs request URLs, which carry the bot
token; yt-dlp errors can quote signed CDN URLs).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASK = "***"

# Bot API token: <bot id>:<35-ish chars>. Also matches the `/bot<token>/` URL form.
_TOKEN = re.compile(r"(?<!\d)(\d{5,}):[A-Za-z0-9_-]{30,}")
# Header-style secrets: "Authorization: Bearer x", "Cookie: a=b; c=d", "Set-Cookie: …".
_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-goog-[\w-]+)"
    r"(\s*[:=]\s*)(['\"]?)[^\r\n'\"]+"
)
# key=value pairs whose key names a secret, in query strings, JSON-ish dicts or logs.
_SECRET_PAIR = re.compile(
    r"(?i)(['\"]?\b(?:token|access_token|refresh_token|password|passwd|secret|api_key|apikey"
    r"|sessionid(?:_ss)?|sid_tt|sid_guard|__secure-[\w-]+|sapisid|ssid|hsid|apisid|login_info"
    r"|sig|signature|lsig|x-expires)['\"]?\s*[:=]\s*['\"]?)([^\s&'\",;}]+)"
)
_URL = re.compile(r"https?://[^\s<>\"'`]+", re.I)

# Query parameters that identify the video (kept); every other value is masked.
_IDENTITY_PARAMS = frozenset({"v"})
_PLATFORM_HOSTS = ("tiktok.com", "youtube.com", "youtu.be")


def _is_platform(host: str) -> bool:
    return any(host == base or host.endswith(f".{base}") for base in _PLATFORM_HOSTS)


def safe_url(url: str) -> str:
    """A URL fit for logs: no credentials, query values masked except video ids.

    Platform page URLs keep `v=` so the log still says which video it was. CDN
    URLs (signed, short-lived download links) lose every query value.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        netloc = host + (f":{parts.port}" if parts.port else "")
    except ValueError:
        return MASK
    keep = _IDENTITY_PARAMS if _is_platform(host) else frozenset()
    query = urlencode(
        [(k, v if k in keep else MASK) for k, v in parse_qsl(parts.query, keep_blank_values=True)],
        safe="*",
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def redact(text: str) -> str:
    """Mask tokens, cookie/authorization headers, secret pairs and URL query values."""
    if not text:
        return text
    text = _TOKEN.sub(lambda m: f"{m.group(1)}:{MASK}", text)
    text = _URL.sub(lambda m: safe_url(m.group(0)), text)
    text = _HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MASK}", text)
    return _SECRET_PAIR.sub(lambda m: f"{m.group(1)}{MASK}", text)
