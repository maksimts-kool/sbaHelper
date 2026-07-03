from __future__ import annotations

import html
import re

INFO_EMOJI = "![ℹ️](tg://emoji?id=5231012545799666522)"
DOWNLOAD_EMOJI = "![⬇️](tg://emoji?id=5386367538735104399)"
SEND_VIDEO_EMOJI = "![📤](tg://emoji?id=5201691993775818138)"
LENGTH_EMOJI_ID = "5350438526691326210"
VIEWS_EMOJI_ID = "5210956306952758910"
LIKES_EMOJI_ID = "5337080053119336309"


def format_duration(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}"


def format_count(value: int | None) -> str:
    if value is None:
        return "—"
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        compact = value / 1_000
        suffix = "K"
    elif value < 1_000_000_000:
        compact = value / 1_000_000
        suffix = "M"
    else:
        compact = value / 1_000_000_000
        suffix = "B"

    formatted = f"{compact:.1f}".rstrip("0").rstrip(".")
    return f"{formatted}{suffix}"


def prevent_auto_links(text: str) -> str:
    sanitized = text.replace("#", "#\u2060").replace("@", "@\u2060")
    sanitized = re.sub(
        r"(?i)\bhttps?://", lambda m: m.group(0)[0] + "\u2060" + m.group(0)[1:], sanitized
    )
    sanitized = re.sub(
        r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b",
        lambda m: m.group(0).replace(":", ":\u2060"),
        sanitized,
    )
    return sanitized


def build_video_caption(info) -> str:
    duration_text = prevent_auto_links(format_duration(info.duration) if info.duration else "0:00")
    title = html.escape(prevent_auto_links(info.title))
    uploader = html.escape(prevent_auto_links(info.uploader))
    lines = [
        f'🎬 <b>{title}</b> | <tg-emoji emoji-id="{LENGTH_EMOJI_ID}">⏱️</tg-emoji> {duration_text}'
    ]

    if info.view_count is not None:
        lines.append(
            f'<tg-emoji emoji-id="{VIEWS_EMOJI_ID}">👁️</tg-emoji> '
            f"{format_count(info.view_count)} просмотров"
        )

    if info.like_count is not None:
        lines.append(
            f'<tg-emoji emoji-id="{LIKES_EMOJI_ID}">❤️</tg-emoji> '
            f"{format_count(info.like_count)} лайков"
        )

    lines.append(f"👤 {uploader}")
    return "\n".join(lines)


def escape_md_v2(text: str) -> str:
    return re.sub(r"([_\*\[\]\(\)~`>#+\-=|{}.!])", r"\\\1", text)


def build_status_text(title: str, uploader: str, duration_str: str, status_line: str) -> str:
    return (
        f"📹 *{escape_md_v2(title)}*\n"
        f"👤 {escape_md_v2(uploader)}{escape_md_v2(duration_str)}\n\n"
        f"{status_line}"
    )


def build_download_progress_line(progress_pct: int) -> str:
    bounded_pct = max(0, min(progress_pct, 100))
    bar = "▓" * (bounded_pct // 10) + "░" * (10 - bounded_pct // 10)
    return f"{DOWNLOAD_EMOJI} Скачиваю: \\[{bar}\\] {bounded_pct}%"
