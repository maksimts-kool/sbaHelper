"""Which links the bot reacts to, where they come from, and what video they name.

Links are read from the message text or caption *and* from its `url` and
`text_link` entities (https://core.telegram.org/bots/api#messageentity), so a
link hidden behind other text counts by its real target. python-telegram-bot's
`parse_entities` handles the UTF-16 offsets. Every candidate is validated
before anything else sees it: http(s) only, a TikTok/YouTube host, no
credentials, no unusual port. Only validated URLs ever reach yt-dlp.

Each link gets a cache identity (`Link.key`):

- `youtube:<id>` / `tiktok:<id>` when the URL itself names the video;
- `url:<host><path>[?query]` for short links (vm.tiktok.com/…) whose video is
  only known after yt-dlp follows them. Query parameters are kept, minus
  well-known tracking ones, so two different videos never share a key.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from telegram import Message

PLATFORM_HOSTS = {
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
}
MAX_URL_LENGTH = 2048

# A loose scan of plain text; every match is validated by `parse_link` anyway.
_LINK = re.compile(
    r"(?<![\w.@/:-])(?:https?://)?(?:[\w-]+\.)*(?:tiktok\.com|youtube\.com|youtu\.be)(?::\d+)?/[^\s<>\"']+",
    re.I,
)
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

_YOUTUBE_ID = r"[A-Za-z0-9_-]{11}"
_YOUTUBE_PATHS = re.compile(rf"^/(?:shorts|embed|live|v|e)/({_YOUTUBE_ID})(?:[/?#]|$)")
_TIKTOK_PATH = re.compile(r"^/(?:@[^/]+/video|v|embed(?:/v2)?)/(\d{5,25})(?:[/.?#]|$)")
_TRACKING_PARAMS = frozenset(
    {
        "si",
        "feature",
        "pp",
        "ab_channel",
        "_t",
        "_r",
        "is_from_webapp",
        "sender_device",
        "web_id",
        "is_copy_url",
        "share_app_id",
        "share_link_id",
        "share_item_id",
        "social_sharing",
        "source",
        "tt_from",
        "u_code",
        "user_id",
        "sec_user_id",
        "checksum",
        "timestamp",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "lang",
        "refer",
        "embed_source",
        "t",
    }
)


@dataclass(frozen=True, slots=True)
class Link:
    url: str
    platform: str
    # Cache / in-flight identity; see the module docstring.
    key: str

    @property
    def resolved(self) -> bool:
        """True when `key` names the video itself, not just this URL."""
        return not self.key.startswith("url:")


def platform_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return "other"
    return _platform_of_host(host)


def _platform_of_host(host: str) -> str:
    for platform, domains in PLATFORM_HOSTS.items():
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return platform
    return "other"


def is_not_a_video(url: str) -> bool:
    return any(pattern.search(url) for pattern in _NOT_A_VIDEO)


def parse_link(raw: str) -> Link | None:
    """A validated, https-normalised link, or None for anything we do not serve."""
    candidate = raw.strip().rstrip(_TRAILING_PUNCTUATION)
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme.lower() not in ("http", "https") or parts.username or parts.password:
        return None
    if port not in (None, 80, 443) or any(ch in host for ch in "%\\@"):
        return None
    platform = _platform_of_host(host)
    if platform == "other":
        return None
    path = parts.path or "/"
    url = urlunsplit(("https", host, path, parts.query, ""))
    return Link(url=url, platform=platform, key=identity(platform, host, path, parts.query))


def identity(platform: str, host: str, path: str, query: str) -> str:
    params = parse_qsl(query, keep_blank_values=True)
    if platform == "youtube":
        if host == "youtu.be" or host.endswith(".youtu.be"):
            match = re.match(rf"^/({_YOUTUBE_ID})(?:[/?#]|$)", path)
            if match:
                return f"youtube:{match[1]}"
        if path.rstrip("/") == "/watch":
            video = dict(params).get("v", "")
            if re.fullmatch(_YOUTUBE_ID, video):
                return f"youtube:{video}"
        if match := _YOUTUBE_PATHS.match(path):
            return f"youtube:{match[1]}"
    elif match := _TIKTOK_PATH.match(path):
        return f"tiktok:{match[1]}"
    kept = sorted((k, v) for k, v in params if k.lower() not in _TRACKING_PARAMS)
    query_part = f"?{urlencode(kept)}" if kept else ""
    # Hosts like www./m. are aliases; short-link hosts (vm., vt.) are not.
    bare = re.sub(r"^(?:www|m)\.", "", host)
    return f"url:{bare}{path.rstrip('/') or '/'}{query_part}"


def video_key(platform: str, video_id: object) -> str | None:
    """The identity yt-dlp's metadata gives after extraction."""
    text = str(video_id or "").strip()
    if platform not in PLATFORM_HOSTS or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", text):
        return None
    return f"{platform}:{text}"


def _candidates_from_text(text: str) -> Iterable[str]:
    for match in _LINK.finditer(text or ""):
        yield match.group(0)


def find_links(texts: Iterable[str], urls: Iterable[str] = ()) -> list[Link]:
    """Supported video links in order of appearance, deduplicated by identity.

    `urls` are entity targets (text_link URLs and url entities); `texts` are
    scanned as a fallback for clients that send no entities.
    """
    seen: set[str] = set()
    found: list[Link] = []
    for raw in [*urls, *(c for text in texts for c in _candidates_from_text(text))]:
        link = parse_link(raw)
        if link is None or is_not_a_video(link.url) or link.key in seen:
            continue
        seen.add(link.key)
        found.append(link)
    return found


def message_links(message: Message) -> list[Link]:
    """Links from a Telegram message: text, caption and their url/text_link entities."""
    from telegram import MessageEntity

    kinds = [MessageEntity.URL, MessageEntity.TEXT_LINK]
    urls: list[str] = []
    for entities in (message.parse_entities(kinds), message.parse_caption_entities(kinds)):
        for entity, visible in entities.items():
            urls.append(entity.url if entity.type == MessageEntity.TEXT_LINK else visible)
    return find_links([message.text or "", message.caption or ""], urls)


def find_link(text: str) -> str | None:
    """The first supported link in plain text (kept for callers without entities)."""
    links = find_links([text])
    return links[0].url if links else None
