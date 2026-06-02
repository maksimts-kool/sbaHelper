from __future__ import annotations

import logging
import os
from collections.abc import Callable

from downloader.platforms import is_facebook_url, is_tiktok_url, is_youtube_url
from downloader.service import COOKIES_FILE, YOUTUBE_COOKIES_FILE


logger = logging.getLogger(__name__)

YTDLP_RETRY_ATTEMPTS = 3
YTDLP_RETRY_MAX_SLEEP_SEC = 8.0


def get_cookie_file_for_url(url: str) -> str:
    if is_youtube_url(url):
        return YOUTUBE_COOKIES_FILE
    return COOKIES_FILE.strip()


def apply_auth_options(ydl_opts: dict, url: str) -> None:
    cookie_file = get_cookie_file_for_url(url)
    if cookie_file:
        if os.path.exists(cookie_file):
            ydl_opts["cookiefile"] = cookie_file
            logger.debug("yt-dlp cookie file enabled: %s", cookie_file)
            if is_youtube_url(url):
                ydl_opts["js_runtimes"] = {"node": {}}
                ydl_opts["remote_components"] = ["ejs:github"]
        else:
            logger.warning("Cookie file is set but not found: %s", cookie_file)
    else:
        logger.debug("yt-dlp cookie file is not configured for this URL.")


def build_ydl_opts(
    url: str,
    *,
    download: bool,
    output_template: str | None = None,
    progress_hook: Callable[[dict], None] | None = None,
) -> dict:
    ydl_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": YTDLP_RETRY_ATTEMPTS,
        "extractor_retries": YTDLP_RETRY_ATTEMPTS,
        "file_access_retries": YTDLP_RETRY_ATTEMPTS,
    }

    if is_tiktok_url(url):
        ydl_opts["extractor_args"] = {"tiktok": {"app_version": ""}}

    if download:
        if not output_template:
            raise ValueError("output_template is required when download=True")
        ydl_opts.update(
            {
                "outtmpl": output_template,
                "format": get_format_selector(url),
                "merge_output_format": "mp4",
            }
        )
        if progress_hook is not None:
            ydl_opts["progress_hooks"] = [progress_hook]
    else:
        ydl_opts["skip_download"] = True
        ydl_opts["format"] = get_metadata_format_selector(url)

    apply_auth_options(ydl_opts, url)
    return ydl_opts


def get_metadata_format_selector(url: str) -> str:
    if is_tiktok_url(url):
        return "best[ext=mp4][height<=1080]/best[height<=1080]/best"

    return (
        "best*[vcodec!=none][height<=1080]/"
        "bestvideo*[vcodec!=none][height<=1080]/"
        "best*[vcodec!=none]/"
        "bestvideo*[vcodec!=none]/"
        "best"
    )


def get_format_selector(url: str) -> str:
    if is_youtube_url(url):
        return (
            "bestvideo[ext=mp4][height<=1080][vcodec!=none]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
            "best[ext=mp4][height<=1080]/best[height<=1080]/best"
        )

    if is_facebook_url(url):
        return (
            "best[ext=mp4][height<=1080]/best[height<=1080]/"
            "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
            "best"
        )

    if is_tiktok_url(url):
        return "best[ext=mp4][height<=1080]/best[height<=1080]/best"

    return (
        "bestvideo[height<=1080][vcodec!=none]+bestaudio/"
        "best[ext=mp4][height<=1080]/best[height<=1080]/best"
    )
