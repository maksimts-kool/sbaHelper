"""Which links the bot reacts to, and which platform they belong to."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

PLATFORM_HOSTS = {
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
}

# Any page on the platform's domain, including subdomains (www, m, vm, vt).
# Whether it is actually a short vertical video is decided from its metadata.
_LINK = re.compile(r"https?://(?:[\w-]+\.)*(?:tiktok\.com|youtube\.com|youtu\.be)/\S+", re.I)
_TRAILING_PUNCTUATION = ".,;:!?)»\"'"

# Profiles, channels, playlists and feeds. Ignored silently so that a profile
# link in chat never starts a download of somebody's whole account.
_NOT_A_VIDEO = (
    re.compile(r"https?://(?:[\w-]+\.)?tiktok\.com/@[^/?#\s]+/?(?:[?#]|$)", re.I),
    re.compile(
        r"https?://(?:[\w-]+\.)?tiktok\.com/"
        r"(?:tag|music|discover|foryou|following|live|search|explore)\b",
        re.I,
    ),
    re.compile(
        r"https?://(?:[\w-]+\.)?youtube\.com/"
        r"(?:@[^/?#\s]+|channel/|c/|user/|playlist\b|feed/|results\b|hashtag/|shorts/?(?:[?#]|$))",
        re.I,
    ),
)


def find_link(text: str) -> str | None:
    """The first TikTok/YouTube link in a message, without trailing punctuation."""
    match = _LINK.search(text)
    return match.group(0).rstrip(_TRAILING_PUNCTUATION) if match else None


def is_not_a_video(url: str) -> bool:
    return any(pattern.search(url) for pattern in _NOT_A_VIDEO)


def platform_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for platform, domains in PLATFORM_HOSTS.items():
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return platform
    return "other"
