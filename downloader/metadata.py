from __future__ import annotations

import re

from downloader.models import VideoInfo
from downloader.platforms import is_facebook_url, is_tiktok_url


def parse_compact_count(raw_value: str) -> int | None:
    cleaned = raw_value.strip().replace(",", "").replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMB])?", cleaned, re.IGNORECASE)
    if not match:
        return None

    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    multiplier = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
    }[suffix]
    return int(number * multiplier)


def pick_first_text(meta: dict, *keys: str) -> str | None:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def looks_like_generated_tiktok_title(title: str | None) -> bool:
    if not title:
        return False
    return bool(re.fullmatch(r"TikTok video #\d+", title.strip(), re.IGNORECASE))


def pick_first_int(meta: dict, *keys: str) -> int | None:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            parsed = parse_compact_count(value)
            if parsed is not None:
                return parsed
    return None


def normalize_video_info(url: str, meta: dict, duration: int) -> VideoInfo:
    title = pick_first_text(meta, "title", "fulltitle", "alt_title") or "Без названия"
    uploader = pick_first_text(
        meta,
        "uploader",
        "channel",
        "creator",
        "artist",
        "channel_follower",
        "uploader_id",
        "channel_id",
    ) or "Неизвестно"
    view_count = pick_first_int(meta, "view_count", "play_count")
    like_count = pick_first_int(meta, "like_count", "repost_count")

    if is_facebook_url(url) and isinstance(title, str):
        parts = [part.strip() for part in title.split("|")]
        if len(parts) >= 3:
            stats_part = parts[0]
            possible_title = " | ".join(part for part in parts[1:-1] if part)
            possible_author = parts[-1]

            view_match = re.search(r"([\d.,KMBkmb]+)\s+views\b", stats_part, re.IGNORECASE)
            reaction_match = re.search(r"([\d.,KMBkmb]+)\s+reactions?\b", stats_part, re.IGNORECASE)

            parsed_view_count = parse_compact_count(view_match.group(1)) if view_match else None
            parsed_like_count = parse_compact_count(reaction_match.group(1)) if reaction_match else None

            if possible_title:
                title = possible_title
            if possible_author and uploader == "Неизвестно":
                uploader = possible_author
            if parsed_view_count is not None and (view_count in (None, 0) or parsed_view_count > view_count):
                view_count = parsed_view_count
            if parsed_like_count is not None and like_count in (None, 0):
                like_count = parsed_like_count

    if is_tiktok_url(url):
        if looks_like_generated_tiktok_title(title):
            better_title = pick_first_text(meta, "description", "fulltitle", "alt_title")
            if better_title:
                title = better_title

        better_uploader = pick_first_text(meta, "channel", "creator", "uploader", "uploader_id")
        if better_uploader:
            uploader = better_uploader

    return VideoInfo(
        title=title,
        uploader=uploader,
        duration=duration,
        thumbnail=meta.get("thumbnail"),
        view_count=view_count,
        like_count=like_count,
    )
