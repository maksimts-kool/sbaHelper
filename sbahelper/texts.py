"""Everything the bot says, in one place. The bot speaks Russian only.

All messages use Telegram HTML. Anything that comes from users or sites
(titles, names) goes through `safe()`: HTML-escaped and defused so it cannot
turn into a link, a mention or a hashtag.

Telegram limits captions to 1024 and messages to 4096 characters *after*
entity parsing (https://core.telegram.org/bots/api#sendvideo,
https://core.telegram.org/bots/api#sendmessage). Renderers shorten the
dynamic parts (titles, names, reasons) as plain text *before* escaping, so a
tag or an `&amp;` is never cut in half, and `fit()` guarantees the final
visible length. Lengths are counted in UTF-16 code units, which is what
Telegram's entity offsets use and never less than the character count.

Sizes shown to people are decimal: 1 МБ = 1,000,000 bytes, matching
MAX_FILE_SIZE_BYTES.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # download.py raises the errors rendered here
    from sbahelper.checks import CheckResult
    from sbahelper.cookies import CookieStatus
    from sbahelper.download import DownloadError, VideoInfo
    from sbahelper.service import ServiceStatus
    from sbahelper.stats import PeriodStats, UserTally

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
TITLE_LIMIT = 60
CAPTION_TITLE_LIMIT = 200
STATUS_TITLE_LIMIT = 120
NAME_LIMIT = 64
REASON_LIMIT = 200


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

COMMANDS = [
    ("start", "Что я умею"),
    ("help", "Справка"),
    ("download", "Скачать видео по ссылке"),
    ("stats", "Статистика чата"),
    ("settings", "Настройки"),
    ("privacy", "Какие данные я храню"),
    ("delete_my_data", "Удалить мою статистику"),
]
ADMIN_COMMANDS = [
    *COMMANDS,
    ("status", "Состояние бота"),
    ("cookies", "Состояние cookies"),
    ("check", "Проверить загрузку"),
]


# --------------------------------------------------------------------------- #
#  Length limits                                                              #
# --------------------------------------------------------------------------- #

_TAG = re.compile(r"<[^>]*>")


def visible_length(text: str) -> int:
    """Length after Telegram parses the HTML, in UTF-16 code units."""
    plain = html.unescape(_TAG.sub("", text))
    return len(plain.encode("utf-16-le")) // 2


def fit(render: Callable[[dict[str, str]], str], fields: dict[str, str], limit: int) -> str:
    """Render, shortening the longest plain-text field until the result fits.

    `render` must escape the fields itself. Fixed parts are assumed to fit.
    """
    fields = dict(fields)
    while True:
        text = render(fields)
        over = visible_length(text) - limit
        if over <= 0:
            return text
        key = max(fields, key=lambda k: len(fields[k]))
        current = fields[key]
        if len(current) <= 1:
            return text
        fields[key] = truncate(current, max(1, len(current) - over - 1))


# --------------------------------------------------------------------------- #
#  Chat messages                                                              #
# --------------------------------------------------------------------------- #


def start(max_minutes: int, max_bytes: int) -> str:
    return (
        "Привет! Я скачиваю короткие вертикальные видео из <b>TikTok</b> "
        "и <b>YouTube Shorts</b>.\n\n"
        "Просто пришли ссылку — в ответ прилетит видео.\n"
        f"• длина — до {max_minutes} мин\n"
        f"• размер — до {format_size(max_bytes)}\n\n"
        "Длинные и горизонтальные ролики, фото и трансляции не качаю.\n"
        "/help — все команды"
    )


def help_text(*, max_links: int, is_group: bool, auto_download: bool) -> str:
    lines = [
        "<b>Команды</b>",
        "/download &lt;ссылка&gt; — скачать видео (или ответь этой командой на сообщение "
        "со ссылкой)",
        "/stats — статистика чата за день, неделю или месяц",
        "/settings — настройки",
        "/privacy — какие данные я храню",
        "/delete_my_data — удалить мою статистику",
        "",
        f"Из одного сообщения беру до {max_links} "
        f"{plural(max_links, 'ссылки', 'ссылок', 'ссылок')}.",
    ]
    if is_group:
        if auto_download:
            lines.append(
                "В этом чате я сам замечаю ссылки в сообщениях. Если не реагирую, значит "
                "Telegram мне их не показывает (режим приватности бота) — тогда помогает "
                "/download."
            )
        else:
            lines.append("Автоскачивание в этом чате выключено — используй /download.")
    return "\n".join(lines)


CHECKING = f"{INFO} Смотрю ссылку…"
QUEUED = f"{INFO} В очереди — скоро начну."
SENDING = f"{UPLOAD} Отправляю…"
STATS_OFF = "Статистика выключена."
DOWNLOAD_USAGE = (
    "Пришли команду со ссылкой: <code>/download https://…</code>, "
    "или ответь /download на сообщение со ссылкой."
)
NO_LINKS = "В этом сообщении нет ссылок на видео из TikTok или YouTube."
UNEXPECTED = "❌ Что-то пошло не так. Попробуй ещё раз."
UPLOAD_UNCERTAIN = (
    "⚠️ Telegram не ответил вовремя — видео могло и отправиться. "
    "Если его нет, пришли ссылку ещё раз."
)

_FIXED_ERRORS = {
    # Rejections: the link works, but it is not something the bot downloads.
    "photo": "🖼 Тут только фото — скачивать нечего.",
    "profile": "👤 Это профиль или подборка, а не видео.",
    "live": "📡 Трансляции не качаю — только обычные видео.",
    "horizontal": "↔️ Качаю только вертикальные видео: Shorts и TikTok.",
    "unsupported": "🤷 Эту ссылку я скачать не умею.",
    # Failures.
    "unavailable": "🚫 Видео недоступно: удалено, скрыто или не показывается в этой стране.",
    "private": "🔐 Это приватное видео — скачать его нельзя.",
    "login": (
        "🔒 Сайт просит войти в аккаунт. Возможно, у бота устарел вход — админы уже "
        "в курсе. Попробуй позже."
    ),
    "blocked": "🛡 TikTok не пустил бота. Попробуй через пару минут.",
    "network": "🌐 Не достучался до сайта. Попробуй чуть позже.",
    "no_audio": "🔇 TikTok отдал видео без звука. Попробуй ещё раз.",
    "media": "🧩 Видео скачалось не полностью или повреждено. Попробуй ещё раз.",
    "tools": "🛠 У бота сломались инструменты для видео. Админы уже в курсе.",
    "timeout": "⌛ Скачивание заняло слишком много времени. Попробуй позже.",
    "cancelled": "♻️ Бот перезапускается — пришли ссылку ещё раз через минуту.",
    # Refusals before any work starts.
    "busy": "⏳ Сейчас слишком много загрузок. Попробуй через пару минут.",
    "user_busy": "⏳ У тебя уже есть видео в работе — дождись их.",
    "chat_busy": "⏳ В этом чате уже много видео в работе — подожди немного.",
    "rate_limited": "🐢 Слишком много ссылок подряд. Подожди минуту.",
    "no_space": "💾 У бота закончилось место для загрузок. Попробуй позже.",
    "shutting_down": "♻️ Бот перезапускается — пришли ссылку ещё раз через минуту.",
}
REFUSAL_CODES = frozenset(
    {"busy", "user_busy", "chat_busy", "rate_limited", "no_space", "shutting_down"}
)


def refusal(code: str) -> str:
    return _FIXED_ERRORS.get(code, UNEXPECTED)


def too_long(duration: int, max_duration: int) -> str:
    return (
        f"⏱ Видео длиной {format_duration(duration)} — слишком длинное. "
        f"Мой предел — {format_total_duration(max_duration)}."
    )


def too_big(size_bytes: int, max_bytes: int) -> str:
    return (
        f"📦 Файл весит {format_size(size_bytes)}, "
        f"а я отправляю видео только до {format_size(max_bytes)}."
    )


def failed(reason: str) -> str:
    return f"❌ Не получилось скачать.\n<code>{html.escape(truncate(reason, REASON_LIMIT))}</code>"


def send_failed(reason: str) -> str:
    return (
        "❌ Не получилось отправить видео.\n"
        f"<code>{html.escape(truncate(reason, REASON_LIMIT))}</code>"
    )


def error_text(error: DownloadError, max_bytes: int) -> str:
    """The chat message for a download error, chosen by its code."""
    params = error.params
    if error.code == "too_long":
        return too_long(int(params.get("duration", 0)), int(params.get("limit", 0)))
    if error.code == "too_big":
        return too_big(int(params.get("size", 0)), max_bytes)
    if error.code in _FIXED_ERRORS:
        return _FIXED_ERRORS[error.code]
    return failed(error.reason)


def too_many_links(limit: int) -> str:
    word = plural(limit, "ссылки", "ссылок", "ссылок")
    return f"ℹ️ Из одного сообщения беру не больше {limit} {word} — остальные пропустил."


# --------------------------------------------------------------------------- #
#  Status message and caption                                                 #
# --------------------------------------------------------------------------- #


def _title(info: VideoInfo) -> str:
    return info.title or "Без названия"


def _uploader(info: VideoInfo) -> str:
    return info.uploader or "Неизвестно"


def status(info: VideoInfo, line: str) -> str:
    def render(f: dict[str, str]) -> str:
        return f"📹 <b>{safe(f['title'])}</b>\n👤 {safe(f['uploader'])} · {_length(info)}\n\n{line}"

    fields = {
        "title": truncate(_title(info), STATUS_TITLE_LIMIT),
        "uploader": truncate(_uploader(info), NAME_LIMIT),
    }
    return fit(render, fields, MESSAGE_LIMIT)


def progress(percent: int) -> str:
    percent = max(0, min(percent, 100))
    return f"{DOWNLOAD} Скачиваю  {bar(percent, 100)}  {percent}%"


def caption(info: VideoInfo) -> str:
    details_tail = [f"{LENGTH} {_length(info)}"]
    if info.view_count is not None:
        details_tail.append(f"{VIEWS} {format_count(info.view_count)}")
    if info.like_count is not None:
        details_tail.append(f"{LIKES} {format_count(info.like_count)}")

    def render(f: dict[str, str]) -> str:
        details = [f"👤 {safe(f['uploader'])}", *details_tail]
        return f"🎬 <b>{safe(f['title'])}</b>\n{' · '.join(details)}"

    fields = {
        "title": truncate(_title(info), CAPTION_TITLE_LIMIT),
        "uploader": truncate(_uploader(info), NAME_LIMIT),
    }
    return fit(render, fields, CAPTION_LIMIT)


def _length(info: VideoInfo) -> str:
    return defuse(format_duration(info.duration))


# --------------------------------------------------------------------------- #
#  Statistics                                                                 #
# --------------------------------------------------------------------------- #

PERIOD_TITLES = {"day": "Сегодня", "week": "Эта неделя", "month": "Этот месяц"}
PERIOD_HITS = {"day": "Хит дня", "week": "Хит недели", "month": "Хит месяца"}
PERIOD_BUTTONS = {"day": "День", "week": "Неделя", "month": "Месяц"}
WEEKLY_TITLE = "Итоги недели"
THIS_WEEK = PERIOD_TITLES["week"]


def weekly_stats(stats: PeriodStats, *, title: str = WEEKLY_TITLE) -> str:
    return period_stats(stats, period="week", title=title)


def period_stats(stats: PeriodStats, *, period: str = "week", title: str | None = None) -> str:
    title = title or PERIOD_TITLES.get(period, THIS_WEEK)
    when = (
        format_day(stats.period_start)
        if period == "day"
        else format_period(stats.period_start, stats.period_end)
    )
    header = f"📊 <b>{title} · {when}</b>"
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
    if stats.anonymous_downloads:
        lines.append(f"   без имени — {stats.anonymous_downloads}")

    if stats.platforms:
        total = sum(tally.downloads for tally in stats.platforms)
        # A quote is not monospace, so columns cannot be padded. Every bar has the
        # same width, though, so starting each row with it lines everything up.
        rows = "\n".join(
            f"{bar(tally.downloads, total)}  |  "
            f"{html.escape(PLATFORMS.get(tally.platform, tally.platform)[:NAME_LIMIT])}"
            f" — {tally.downloads}"
            for tally in stats.platforms
        )
        lines += ["", "📱 <b>Площадки</b>", f"<blockquote>{rows}</blockquote>"]

    if stats.top_video is not None:
        video_title = safe(truncate(stats.top_video.title or "Без названия", TITLE_LIMIT))
        views = format_count(stats.top_video.view_count)
        hit = PERIOD_HITS.get(period, PERIOD_HITS["week"])
        lines += ["", f"🔥 <b>{hit}</b>", f"«{video_title}» — {views} 👁"]

    return "\n".join(lines)


def user_label(tally: UserTally) -> str:
    """A real `@username` becomes a mention that shows the person's current name.

    Without one, the name saved at download time is used, defused so a display
    name like "@everyone" cannot ping anybody. Names made of invisible
    characters get a readable placeholder.
    """
    if tally.username:
        return f"@{html.escape(tally.username[:NAME_LIMIT])}"
    if has_visible_text(tally.name):
        return safe(truncate(tally.name, NAME_LIMIT))
    return f"Участник {tally.user_id}" if tally.user_id else "Неизвестно"


# --------------------------------------------------------------------------- #
#  Settings, privacy and personal data                                        #
# --------------------------------------------------------------------------- #

ON, OFF = "вкл", "выкл"
SAVED = "Сохранено"
STALE_BUTTON = "Эта кнопка устарела."
NOT_YOURS = "Эта кнопка не для тебя."
NOT_AVAILABLE = "Недоступно."
NOT_GROUP_ADMIN = "Это могут менять только администраторы группы."
ANONYMOUS_ADMIN = "Анонимного админа я проверить не могу — выключи анонимность и попробуй снова."
BTN_DELETE = "🗑 Удалить"
BTN_CANCEL = "Отмена"
BTN_SUMMARY_HERE = "📌 Итоги — в эту тему"
BTN_SUMMARY_MAIN = "↩️ Итоги — в основной чат"


def btn_stats(enabled: bool) -> str:
    return f"📊 Учитывать меня в статистике: {ON if enabled else OFF}"


def btn_auto(enabled: bool) -> str:
    return f"🔗 Сам скачивать ссылки: {ON if enabled else OFF}"


def personal_settings(*, counted: bool) -> str:
    state = "да" if counted else "нет — мои видео считаются без имени"
    return (
        "⚙️ <b>Твои настройки</b>\n\n"
        f"Учитывать меня в статистике чатов: <b>{state}</b>\n\n"
        "Удалить уже собранную статистику: /delete_my_data"
    )


def group_settings(*, auto_download: bool, summary_topic: int | None, in_topic: bool) -> str:
    auto = "да" if auto_download else "нет — только по /download"
    topic = "в выбранную тему" if summary_topic else "в основной чат"
    lines = [
        "⚙️ <b>Настройки чата</b>",
        "",
        f"Сам скачивать ссылки из сообщений: <b>{auto}</b>",
        f"Итоги недели приходят: <b>{topic}</b>",
        "",
        "Менять могут администраторы группы. Личные настройки — в личке со мной.",
    ]
    if not in_topic and not summary_topic:
        lines.append("Чтобы итоги приходили в тему форума, вызови /settings в этой теме.")
    return "\n".join(lines)


def privacy(*, retention_days: int, cache_days: int) -> str:
    kept = (
        f"хранится {retention_days} {plural(retention_days, 'день', 'дня', 'дней')}"
        if retention_days
        else "хранится бессрочно"
    )
    cache = (
        f"хранится до {cache_days} {plural(cache_days, 'дня', 'дней', 'дней')}"
        if cache_days
        else "кэш выключен"
    )
    return (
        "🔐 <b>Какие данные я храню</b>\n\n"
        "• <b>Статистика</b>: за каждое отправленное видео — чат, твой Telegram id, имя и "
        "@username, площадка, название, автор, длина, размер и просмотры ролика, время. Нужна "
        f"для /stats и итогов недели; {kept}.\n"
        "• <b>Кэш видео</b>: Telegram-идентификатор уже отправленного файла и данные ролика, "
        f"без сведений о том, кто его просил; {cache}.\n"
        "• <b>Настройки</b>: твой отказ от статистики; для групп — их настройки.\n"
        "• <b>Журналы работы</b>: id чата и пользователя и ссылка, чтобы разбирать ошибки. "
        "Хранятся ограниченное время и в /delete_my_data не входят.\n\n"
        "Сами видеофайлы я удаляю с сервера сразу после отправки.\n\n"
        "/settings — не учитывать тебя в статистике\n"
        "/delete_my_data — удалить твою статистику во всех чатах и твои настройки. Уже "
        "отправленные сообщения, журналы и резервные копии это не удаляет."
    )


def delete_confirm(count: int) -> str:
    records = plural(count, "запись", "записи", "записей")
    return (
        f"Удалить твою статистику во всех чатах ({count} {records}) и твои настройки?\n\n"
        "Это нельзя отменить. Уже отправленные сообщения, журналы и резервные копии "
        "не затрагиваются."
    )


def delete_done(count: int) -> str:
    records = plural(count, "запись", "записи", "записей")
    return f"Готово: удалено {count} {records}, настройки сброшены."


DELETE_CANCELLED = "Ничего не удалил."
DATA_UNAVAILABLE = "База данных сейчас недоступна — попробуй позже."


# --------------------------------------------------------------------------- #
#  Admin tools                                                                #
# --------------------------------------------------------------------------- #

COOKIES_HINT = (
    "Чтобы обновить, пришли мне файл cookies: <code>cookies.txt</code> в формате Netscape "
    "или JSON из расширения Cookie-Editor. Можно один файл сразу со всеми сайтами.\n\n"
    "<b>YouTube</b>: открой окно инкогнито, войди в аккаунт, перейди на "
    "<code>youtube.com/robots.txt</code>, выгрузи cookies и сразу закрой окно. Если этот вход "
    "потом открыть в браузере, YouTube отзовёт cookies за несколько часов. Лучше запасной аккаунт."
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
        lines.append(f"   {_cookie_health(item, now)}")
    return "\n".join([*lines, "", COOKIES_HINT])


def _cookie_health(item: CookieStatus, now: datetime) -> str:
    """What the site said last time: the expiry date in the file cannot tell."""
    if item.health is None:
        return "❔ ещё не проверялись — /check"
    at = item.health.at.astimezone(now.tzinfo)
    when = f"{_day_month(at)} в {at:%H:%M}"
    if item.health.ok:
        return f"✅ работают, последний раз {when}"
    return f"❌ сайт просит войти заново ({when}) — пришли свежие cookies"


def _days_left(item: CookieStatus, now: datetime) -> str:
    assert item.login_expires is not None
    days = (item.login_expires - now).days
    return "сегодня" if days < 1 else f"через {days} {plural(days, 'день', 'дня', 'дней')}"


def cookies_saved(counts: dict[str, int], *, deleted: bool = True) -> str:
    lines = ["🍪 Cookies сохранены:"]
    lines += [f"• {PLATFORMS.get(p, p)} — {n} шт." for p, n in counts.items()]
    lines.append("")
    if deleted:
        lines.append("Сообщение с файлом удалил. Проверить загрузку: /check")
    else:
        lines.append(
            "⚠️ Не смог удалить сообщение с файлом — удали его сам: в нём твой вход. "
            "Проверить загрузку: /check"
        )
    return "\n".join(lines)


def cookies_rejected(reason: str) -> str:
    return (
        f"Не получилось прочитать cookies: {html.escape(truncate(reason, REASON_LIMIT))}\n\n"
        f"{COOKIES_HINT}"
    )


def check_results(results: list[CheckResult]) -> str:
    if not results:
        return CHECKS_NONE
    lines = [
        f"{'✅' if result.ok else '❌'} <b>{PLATFORMS.get(result.platform, result.platform)}</b>"
        f" — {safe(truncate(result.detail, REASON_LIMIT))}"
        for result in results
    ]
    return "\n".join(["🔎 <b>Проверка загрузки</b>", "", *lines])


def bot_status(status: ServiceStatus, now: datetime) -> str:
    """/status: measured values only; anything unknown says so."""
    yes_no = {True: "✅", False: "❌", None: "❔"}
    free = format_size(status.temp_free_bytes) if status.temp_free_bytes is not None else "?"
    lines = [
        "🩺 <b>Состояние бота</b>",
        "",
        f"Работает: {format_total_duration(status.uptime_sec)}"
        + (" · <b>останавливается</b>" if status.shutting_down else ""),
        f"Загрузки: {status.active} идёт · {status.queued} в очереди "
        f"(лимит {status.max_active} + {status.max_queued})",
        f"Кэш: {status.cache_hits} попаданий · {status.cache_misses} промахов · "
        f"{status.cache_invalidated} сброшено · {status.cache_entries} записей",
        f"Флуд-контроль: {status.flood_waits} ожиданий · {status.throttled} пропущенных правок",
        f"Место для загрузок: {free} свободно",
        f"База данных: {yes_no[status.database_ok]}",
        f"Доступ: {html.escape(status.access)}",
        f"Режим приватности: {html.escape(status.privacy_mode)}",
        "",
        "<b>Площадки</b>",
    ]
    for platform, item in status.platforms.items():
        name = PLATFORMS.get(platform, platform)
        cookies = "нет cookies"
        if item.cookie_health is not None:
            at = item.cookie_health.at.astimezone(now.tzinfo)
            verdict = "приняты" if item.cookie_health.ok else "сайт просит вход"
            cookies = f"cookies {verdict} {_day_month(at)} {at:%H:%M}"
        elif item.has_cookies:
            cookies = "cookies ещё не проверялись"
        check = "проверка не запускалась"
        if item.last_check_ok is not None and item.last_check_at is not None:
            at = item.last_check_at.astimezone(now.tzinfo)
            mark = "✅" if item.last_check_ok else "❌"
            check = f"проверка {mark} {_day_month(at)} {at:%H:%M}"
        lines.append(f"• <b>{name}</b>: {cookies}; {check}")
    return "\n".join(lines)


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
# Never end a shortened text on these: they belong to the character before them.
_JOINERS = "\u200d\ufe0e\ufe0f"


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
    seconds = max(0, int(seconds))
    days, hours, minutes = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    if days:
        return f"{days} д {hours} ч" if hours else f"{days} д"
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    return f"{minutes} мин" if minutes else f"{seconds} сек"


def format_size(size_bytes: int) -> str:
    """Decimal units: 1 МБ = 1,000,000 bytes, 1 ГБ = 1,000 МБ."""
    megabytes = max(0, size_bytes) / 1_000_000
    if megabytes >= 1000:
        return f"{megabytes / 1000:.1f} ГБ"
    return f"{megabytes:.0f} МБ" if megabytes >= 10 or megabytes == 0 else f"{megabytes:.1f} МБ"


def format_period(start: datetime, end: datetime) -> str:
    if (start.year, start.month) == (end.year, end.month):
        if start.day == end.day:
            return format_day(start)
        return f"{start.day}–{end.day} {_MONTHS[end.month - 1]}"
    return f"{_day_month(start)} – {_day_month(end)}"


def format_day(moment: datetime) -> str:
    return _day_month(moment)


def _day_month(moment: datetime) -> str:
    return f"{moment.day} {_MONTHS[moment.month - 1]}"


def plural(count: int, one: str, few: str, many: str) -> str:
    """1 участник, 2 участника, 5 участников."""
    if count % 100 // 10 == 1:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(count % 10, many)


def truncate(text: str, limit: int) -> str:
    """Collapse whitespace and cut to `limit` characters, ending with "…".

    Python strings index code points, so a surrogate pair is never split; a
    trailing zero-width joiner or variation selector is dropped as well.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[: max(0, limit - 1)].rstrip().rstrip(_JOINERS)
    while cut and unicodedata.category(cut[-1]) in ("Mn", "Me"):
        cut = cut[:-1]
    return cut + "…"


def bar(count: int, total: int, width: int = BAR_WIDTH) -> str:
    """`width` blocks showing count/total; any non-zero share gets at least one block."""
    filled = 0 if total <= 0 or count <= 0 else min(width, max(1, round(count / total * width)))
    return "▓" * filled + "░" * (width - filled)
