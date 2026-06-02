from __future__ import annotations


def is_facebook_url(url: str) -> bool:
    lowered = url.lower()
    return "facebook.com" in lowered or "fb.watch" in lowered


def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in url.lower()


def is_youtube_url(url: str) -> bool:
    lowered = url.lower()
    return "youtube.com/" in lowered or "youtu.be/" in lowered
