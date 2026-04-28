"""
Распознавание поддерживаемых ссылок для downloader-бота.
"""
import re

_FACEBOOK_HOST = r"(?:[\w-]+\.)?facebook\.com"
_FACEBOOK_PATH_SUFFIX = r"(?:[/?][^\s]*)?"
_YOUTUBE_HOST = r"(?:(?:www|m)\.)?youtube\.com"

SUPPORTED_URL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"https?://vm\.tiktok\.com/\S+", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?tiktok\.com/\S+", re.IGNORECASE),
    re.compile(rf"https?://{_YOUTUBE_HOST}/shorts/\S+", re.IGNORECASE),
    re.compile(
        rf"https?://{_YOUTUBE_HOST}/watch\?(?=[^\s#]*\bv=[^&#\s]+)[^\s#]+",
        re.IGNORECASE,
    ),
    re.compile(r"https?://youtu\.be/[^/?#\s]+(?:[/?][^\s]*)?", re.IGNORECASE),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/watch(?:/live)?/\?(?=[^\s#]*\b(?:v|video_id)=[^&#\s]+)[^\s#]+",
        re.IGNORECASE,
    ),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/video(?:/video)?\.php\?(?=[^\s#]*\b(?:v|video_id)=[^&#\s]+)[^\s#]+",
        re.IGNORECASE,
    ),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/story\.php\?(?=[^\s#]*\bstory_fbid=[^&#\s]+)[^\s#]+",
        re.IGNORECASE,
    ),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/reel/[^/?#\s]+{_FACEBOOK_PATH_SUFFIX}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/(?:[^/?#\s]+/)?videos/(?:[^/?#\s]+/)?[^/?#\s]+{_FACEBOOK_PATH_SUFFIX}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"https?://{_FACEBOOK_HOST}/share/(?:r|v)/[^/?#\s]+{_FACEBOOK_PATH_SUFFIX}",
        re.IGNORECASE,
    ),
    re.compile(r"https?://fb\.watch/[^/?#\s]+(?:[/?][^\s]*)?", re.IGNORECASE),
]


def extract_supported_url(text: str) -> str | None:
    """Возвращает первую поддерживаемую ссылку из текста, или None."""
    for pattern in SUPPORTED_URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).rstrip(".,);")
    return None
