"""Everything the bot says, in one place.

All messages use Telegram HTML. Anything that comes from users or sites
(titles, names) goes through `safe()`: HTML-escaped and defused so it cannot
turn into a link, a mention or a hashtag.
"""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # download.py imports this module for its error messages
    from sbahelper.checks import CheckResult
    from sbahelper.cookies import CookieStatus
    from sbahelper.download import VideoInfo
    from sbahelper.stats import UserTally, WeeklyStats


def _emoji(emoji_id: str, fallback: str) -> str:
    """Custom emoji; clients that cannot show it fall back to the plain one."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


INFO = _emoji("5231012545799666522", "ℹ️")
DOWNLOAD = _emoji("5386367538735104399", "⬇️")
UPLOAD = _emoji("5201691993775818138", "📤")
LENGTH = _emoji("5350438526691326210", "⏱")
VIEWS = _emoji("5210956306952758910", "👁")
LIKES = _emoji("5337080053119336309", "❤️")

PLATFORMS = {"tiktok": "TikTok", "youtube": "YouTube", "facebook": "Facebook"}
MEDALS = ("🥇", "🥈", "🥉")
BAR_WIDTH = 10
TITLE_LIMIT = 60

COMMANDS = [
    ("start", "Что я умею"),
    ("stats", "Статистика чата за неделю"),
]
ADMIN_COMMANDS = [
    *COMMANDS,
    ("cookies", "Состояние cookies"),
    ("check", "Проверить загрузку"),
]


# --------------------------------------------------------------------------- #
#  Chat messages                                                              #
# --------------------------------------------------------------------------- #


def start(max_minutes: int, max_mb: int) -> str:
    return (
        "Привет! Я скачиваю короткие вертикальные видео из <b>TikTok</b> "
        "и <b>YouTube Shorts</b>.\n\n"
        "Просто пришли ссылку — в ответ прилетит видео.\n"
        f"• длина — до {max_minutes} мин\n"
        f"• размер — до {max_mb} МБ\n\n"
        "Длинные и горизонтальные ролики, фото и трансляции не качаю.\n"
        "/stats — кто сколько скачал за неделю"
    )


CHECKING = f"{INFO} Смотрю ссылку…"
SENDING = f"{UPLOAD} Отправляю…"
STATS_OFF = "Статистика выключена."

# Rejections: the link works, but it is not something the bot downloads.
PHOTO = "🖼 Тут только фото — скачивать нечего."
PROFILE = "👤 Это профиль или подборка, а не видео."
LIVE = "📡 Трансляции не качаю — только обычные видео."
HORIZONTAL = "↔️ Качаю только вертикальные видео: Shorts и TikTok."

# Failures.
LOGIN = "🔒 Сайт просит войти в аккаунт. Админ уже в курсе — попробуй позже."
BLOCKED = "🛡 TikTok не пустил бота. Попробуй через пару минут."
NETWORK = "🌐 Не достучался до сайта. Попробуй чуть позже."
NO_AUDIO = "🔇 TikTok отдал видео без звука. Попробуй ещё раз."
NOT_FOUND = "❌ Видео скачалось, но файл потерялся. Попробуй ещё раз."
UNEXPECTED = "❌ Что-то пошло не так. Попробуй ещё раз."


def too_long(duration: int, max_duration: int) -> str:
    return (
        f"⏱ Видео длиной {format_duration(duration)} — слишком длинное. "
        f"Мой предел — {max_duration // 60} мин."
    )


def too_big(size_mb: float, max_mb: int) -> str:
    return f"📦 Файл весит {size_mb:.1f} МБ, а Telegram пропускает только до {max_mb} МБ."


def failed(reason: str) -> str:
    return f"❌ Не получилось скачать.\n<code>{html.escape(reason)}</code>"


def send_failed(reason: str) -> str:
    return f"❌ Не получилось отправить видео.\n<code>{html.escape(reason)}</code>"


# --------------------------------------------------------------------------- #
#  Status message and caption                                                 #
# --------------------------------------------------------------------------- #


def status(info: VideoInfo, line: str) -> str:
    return f"📹 <b>{safe(info.title)}</b>\n👤 {safe(info.uploader)} · {_length(info)}\n\n{line}"


def progress(percent: int) -> str:
    percent = max(0, min(percent, 100))
    return f"{DOWNLOAD} Скачиваю  {bar(percent, 100)}  {percent}%"


def caption(info: VideoInfo) -> str:
    details = [f"👤 {safe(info.uploader)}", f"{LENGTH} {_length(info)}"]
    if info.view_count is not None:
        details.append(f"{VIEWS} {format_count(info.view_count)}")
    if info.like_count is not None:
        details.append(f"{LIKES} {format_count(info.like_count)}")
    return f"🎬 <b>{safe(info.title)}</b>\n{' · '.join(details)}"


def _length(info: VideoInfo) -> str:
    return defuse(format_duration(info.duration))


# --------------------------------------------------------------------------- #
#  Weekly statistics                                                          #
# --------------------------------------------------------------------------- #


WEEKLY_TITLE = "Итоги недели"
THIS_WEEK = "Эта неделя"


def weekly_stats(stats: WeeklyStats, *, title: str = WEEKLY_TITLE) -> str:
    header = f"📊 <b>{title} · {format_period(stats.period_start, stats.period_end)}</b>"
    if not stats.total_downloads:
        return f"{header}\n\nПока тихо — ни одного видео."

    duration = format_total_duration(stats.total_duration_sec)
    size = format_size(stats.total_size_bytes)
    lines = [header, "", f"Скачано <b>{stats.total_downloads}</b> видео · {duration} · {size}"]

    if stats.top_users:
        lines += ["", "🏆 <b>Самые активные</b>"]
        for index, tally in enumerate(stats.top_users):
            medal = MEDALS[index] if index < len(MEDALS) else "•"
            lines.append(f"{medal} {user_label(tally)} — {tally.downloads}")
        if stats.other_users:
            people = plural(stats.other_users, "участник", "участника", "участников")
            lines.append(f"   и ещё {stats.other_users} {people} — {stats.other_downloads}")

    if stats.platforms:
        total = sum(tally.downloads for tally in stats.platforms)
        # A quote is not monospace, so columns cannot be padded. Every bar has the
        # same width, though, so starting each row with it lines everything up.
        rows = "\n".join(
            f"{bar(tally.downloads, total)}  |  "
            f"{html.escape(PLATFORMS.get(tally.platform, tally.platform))} — {tally.downloads}"
            for tally in stats.platforms
        )
        lines += ["", "📱 <b>Площадки</b>", f"<blockquote>{rows}</blockquote>"]

    if stats.top_video is not None:
        video_title = safe(truncate(stats.top_video.title, TITLE_LIMIT))
        views = format_count(stats.top_video.view_count)
        lines += ["", "🔥 <b>Хит недели</b>", f"«{video_title}» — {views} 👁"]

    return "\n".join(lines)


def user_label(tally: UserTally) -> str:
    """A real `@username` becomes a mention that shows the person's current name.

    Without one, the name saved at download time is used, defused so a display
    name like "@everyone" cannot ping anybody. Names made of invisible
    characters get a readable placeholder.
    """
    if tally.username:
        return f"@{html.escape(tally.username)}"
    if has_visible_text(tally.name):
        return safe(tally.name)
    return f"Участник {tally.user_id}" if tally.user_id else "Неизвестно"


# --------------------------------------------------------------------------- #
#  Admin tools                                                                #
# --------------------------------------------------------------------------- #

COOKIES_HINT = (
    "Чтобы обновить, пришли мне файл cookies: <code>cookies.txt</code> в формате Netscape "
    "или JSON из расширения Cookie-Editor. Можно один файл сразу со всеми сайтами."
)
CHECKS_RUNNING = "🔎 Скачиваю тестовые видео…"
CHECKS_NONE = "Проверки не настроены: задай CHECK_TIKTOK_URL и/или CHECK_YOUTUBE_URL."


def cookies_status(statuses: list[CookieStatus], now: datetime) -> str:
    lines = ["🍪 <b>Cookies</b>", ""]
    for item in statuses:
        name = PLATFORMS.get(item.platform, item.platform)
        if not item.count:
            lines.append(f"• <b>{name}</b> — нет")
            continue
        login = "без входа в аккаунт"
        if item.logged_in:
            login = "вход выполнен"
            if item.login_expires:
                login += f", истекает {_day_month(item.login_expires)} ({_days_left(item, now)})"
        elif item.login_expires:
            login = "вход истёк"
        lines.append(f"• <b>{name}</b> — {item.count} шт., {login}")
    return "\n".join([*lines, "", COOKIES_HINT])


def _days_left(item: CookieStatus, now: datetime) -> str:
    assert item.login_expires is not None
    days = (item.login_expires - now).days
    return "сегодня" if days < 1 else f"через {days} {plural(days, 'день', 'дня', 'дней')}"


def cookies_saved(counts: dict[str, int]) -> str:
    lines = ["🍪 Cookies сохранены:"]
    lines += [f"• {PLATFORMS.get(p, p)} — {n} шт." for p, n in counts.items()]
    lines += ["", "Сообщение с файлом удалил. Проверить загрузку: /check"]
    return "\n".join(lines)


def cookies_rejected(reason: str) -> str:
    return f"Не получилось прочитать cookies: {html.escape(reason)}\n\n{COOKIES_HINT}"


def check_results(results: list[CheckResult]) -> str:
    if not results:
        return CHECKS_NONE
    lines = [
        f"{'✅' if result.ok else '❌'} <b>{PLATFORMS.get(result.platform, result.platform)}</b>"
        f" — {safe(result.detail)}"
        for result in results
    ]
    return "\n".join(["🔎 <b>Проверка загрузки</b>", "", *lines])


# --------------------------------------------------------------------------- #
#  Helpers                                                                    #
# --------------------------------------------------------------------------- #

_MONTHS = (
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

# Spaces, control/format characters and zero-width marks all render as nothing.
_BLANK_CATEGORIES = frozenset({"Cc", "Cf", "Mn", "Me", "Zs", "Zl", "Zp"})
# Unicode calls these letters or symbols, yet their glyph is empty. They are what
# "invisible" Telegram names are made of. Written as escapes: they are invisible here too.
# HANGUL CHOSEONG/JUNGSEONG FILLER, HANGUL FILLER, HALFWIDTH HANGUL FILLER,
# BRAILLE PATTERN BLANK, MONGOLIAN VOWEL SEPARATOR, KHMER INHERENT AQ/AA.
_BLANK_CHARS = frozenset("\u115f\u1160\u3164\uffa0\u2800\u180e\u17b4\u17b5")


def has_visible_text(value: str) -> bool:
    """Would at least one visible character remain after dropping blank glyphs?

    `str.strip()` is not enough: HANGUL FILLER is a letter (`Lo`) and
    VARIATION SELECTOR-15 a combining mark, and `isspace()` is False for both.
    """
    return any(
        char not in _BLANK_CHARS and unicodedata.category(char) not in _BLANK_CATEGORIES
        for char in value
    )


def defuse(text: str) -> str:
    """Insert WORD JOINERs so Telegram does not auto-link URLs, @mentions, #tags or times."""
    text = text.replace("#", "#\u2060").replace("@", "@\u2060")
    text = re.sub(r"(?i)\b(h)(ttps?://)", "\\1\u2060\\2", text)
    return re.sub(r"\b(\d{1,2}):(\d{2})\b", "\\1:\u2060\\2", text)


def safe(text: str) -> str:
    return html.escape(defuse(text))


def format_duration(seconds: int) -> str:
    minutes, seconds = divmod(max(0, seconds), 60)
    return f"{minutes}:{seconds:02d}"


def format_count(value: int) -> str:
    """1234 → 1.2K, 2400000 → 2.4M."""
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= limit:
            return f"{value / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def format_total_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    return f"{minutes} мин" if minutes else f"{seconds} сек"


def format_size(size_bytes: int) -> str:
    megabytes = max(0, size_bytes) / 1024**2
    if megabytes >= 1024:
        return f"{megabytes / 1024:.1f} ГБ"
    return f"{megabytes:.0f} МБ" if megabytes >= 10 or megabytes == 0 else f"{megabytes:.1f} МБ"


def format_period(start: datetime, end: datetime) -> str:
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day} {_MONTHS[end.month - 1]}"
    return f"{_day_month(start)} – {_day_month(end)}"


def _day_month(moment: datetime) -> str:
    return f"{moment.day} {_MONTHS[moment.month - 1]}"


def plural(count: int, one: str, few: str, many: str) -> str:
    """1 участник, 2 участника, 5 участников."""
    if count % 100 // 10 == 1:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(count % 10, many)


def truncate(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def bar(count: int, total: int, width: int = BAR_WIDTH) -> str:
    """`width` blocks showing count/total; any non-zero share gets at least one block."""
    filled = 0 if total <= 0 or count <= 0 else min(width, max(1, round(count / total * width)))
    return "▓" * filled + "░" * (width - filled)
