from __future__ import annotations

import html
import re
from datetime import datetime

from downloader.stats import WeeklyStats

INFO_EMOJI = "![ℹ️](tg://emoji?id=5231012545799666522)"
DOWNLOAD_EMOJI = "![⬇️](tg://emoji?id=5386367538735104399)"
SEND_VIDEO_EMOJI = "![📤](tg://emoji?id=5201691993775818138)"
LENGTH_EMOJI_ID = "5350438526691326210"
VIEWS_EMOJI_ID = "5210956306952758910"
LIKES_EMOJI_ID = "5337080053119336309"

MEDALS = ("🥇", "🥈", "🥉")
PLATFORM_TITLES = {
    "tiktok": "TikTok",
    "youtube": "YouTube",
    # Facebook больше не поддерживается, но старые записи в статистике остаются.
    "facebook": "Facebook",
    "other": "Другое",
}
PLATFORM_BAR_WIDTH = 10
STATS_TITLE_LIMIT = 60

_MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


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


# --------------------------------------------------------------------------- #
#  Недельная статистика                                                        #
# --------------------------------------------------------------------------- #


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу: 1 участник, 2 участника, 5 участников."""
    if count % 100 // 10 == 1:
        return many
    remainder = count % 10
    if remainder == 1:
        return one
    if 2 <= remainder <= 4:
        return few
    return many


def format_total_duration(total_seconds: int) -> str:
    bounded = max(0, total_seconds)
    hours, remainder = divmod(bounded, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    if minutes:
        return f"{minutes} мин"
    return f"{bounded} сек"


def format_size(total_bytes: int) -> str:
    megabytes = max(0, total_bytes) / (1024 * 1024)
    if megabytes >= 1024:
        return f"{megabytes / 1024:.1f} ГБ"
    if megabytes >= 10 or megabytes == 0:
        return f"{megabytes:.0f} МБ"
    return f"{megabytes:.1f} МБ"


def format_period(start: datetime, end: datetime) -> str:
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day} {_MONTHS_GENITIVE[end.month - 1]}"
    return (
        f"{start.day} {_MONTHS_GENITIVE[start.month - 1]} – "
        f"{end.day} {_MONTHS_GENITIVE[end.month - 1]}"
    )


def truncate(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def build_platform_bar(count: int, total: int, width: int = PLATFORM_BAR_WIDTH) -> str:
    """Полоса на `width` блоков — доля площадки от всех загрузок за период."""
    if total <= 0 or count <= 0:
        return "░" * width
    # Хотя бы один блок, чтобы редкая площадка не выглядела как ноль.
    filled = min(width, max(1, round(count / total * width)))
    return "▓" * filled + "░" * (width - filled)


def _build_platform_block(stats: WeeklyStats) -> str:
    total = sum(tally.downloads for tally in stats.platforms)
    name_width = max(len(PLATFORM_TITLES.get(t.platform, t.platform)) for t in stats.platforms)
    rows = "\n".join(
        f"{PLATFORM_TITLES.get(tally.platform, tally.platform):<{name_width}}  "
        f"{build_platform_bar(tally.downloads, total)} {tally.downloads:>3}"
        for tally in stats.platforms
    )
    return f"<pre>{html.escape(rows)}</pre>"


def build_weekly_stats_message(stats: WeeklyStats, *, title: str = "Итоги недели") -> str:
    """Сводка за неделю в HTML: сколько, кто больше всех, откуда и хит недели."""
    period = format_period(stats.period_start, stats.period_end)
    header = f"📊 <b>{html.escape(title)} · {period}</b>"

    if not stats.total_downloads:
        return f"{header}\n\nНа этой неделе тихо — ни одного видео."

    lines = [
        header,
        "",
        f"Скачано: <b>{stats.total_downloads}</b> видео · "
        f"{format_total_duration(stats.total_duration_sec)} · "
        f"{format_size(stats.total_size_bytes)}",
    ]

    if stats.top_users:
        lines += ["", "🏆 <b>Кто больше всех</b>"]
        for index, tally in enumerate(stats.top_users):
            medal = MEDALS[index] if index < len(MEDALS) else "•"
            name = html.escape(prevent_auto_links(tally.name))
            lines.append(f"{medal} {name} — {tally.downloads}")
        if stats.other_users:
            people = plural_ru(stats.other_users, "участник", "участника", "участников")
            lines.append(f"   ещё {stats.other_users} {people} — {stats.other_downloads}")

    if stats.platforms:
        lines += ["", "📱 <b>Откуда</b>", _build_platform_block(stats)]

    if stats.top_video is not None:
        short_title = truncate(stats.top_video.title, STATS_TITLE_LIMIT)
        video_title = html.escape(prevent_auto_links(short_title))
        lines += [
            "",
            "🔥 <b>Хит недели</b>",
            f"«{video_title}» — {format_count(stats.top_video.view_count)} 👁",
        ]

    return "\n".join(lines)
