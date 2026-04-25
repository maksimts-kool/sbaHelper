"""
Функции форматирования текста и клавиатуры для сообщений бота.
"""
import time as time_module
from datetime import date, datetime, timedelta

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from analytics import engine as analytics_engine
from bot.state import VOTE_STATE, get_all_votes_data, get_skip_progress, get_song_votes, is_song_in_best
from core.config import IGNORED_KEYWORDS, REQUEST_URL, STREAM_URL, TZ_NAME, UPVOTE_THRESHOLD
from services.playlist_names import PLAYLIST_NAMES


_RADIO_SHUTDOWN_DATE = date(2026, 4, 27)
_RADIO_SHUTDOWN_EMOJI = "![❗️](tg://emoji?id=5274099962655816924)"
_RADIO_COUNTDOWN_EMOJI = "![⏳](tg://emoji?id=5382194935057372936)"
_SCHEDULE_CURRENT_EMOJI = "![😀](tg://emoji?id=5929468240668397096)"


# --- УТИЛИТЫ ---

def escape_md(text: str) -> str:
    """Экранирует спецсимволы для обычного Markdown (не V2)."""
    if not text:
        return ""
    for char in ['_', '*', '`', '[', ']']:
        text = text.replace(char, f"\\{char}")
    return text


def escape_md_v2(text: str) -> str:
    """Экранирует спецсимволы для MarkdownV2."""
    if not text:
        return ""
    for char in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(char, f"\\{char}")
    return text


def clean_track_info(artist: str, title: str, raw_text: str) -> str:
    """Убирает дублирование, например: 'Artist - Song - Song' -> 'Artist - Song'."""
    if artist and title:
        if title == artist:
            return title
        final_str = f"{artist} - {title}"
    else:
        final_str = raw_text

    if " - " in final_str:
        parts = final_str.split(" - ")
        if len(parts) >= 2 and parts[-1] == parts[-2]:
            parts.pop()
            final_str = " - ".join(parts)

    return final_str


def format_duration(seconds) -> str:
    if not seconds:
        return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02}:{s:02}"


def create_progress_bar(elapsed, total, length=10) -> str:
    """Генерирует строку вида: `01:20 ━━🔘━━━━━━ 03:45`"""
    if total == 0:
        return ""
    percent = min(elapsed / total, 1.0)
    filled_length = int(length * percent)
    bar = '━' * filled_length + '🔘' + '─' * (length - filled_length)
    return f"`{format_duration(elapsed)} {bar} {format_duration(total)}`"


def get_radio_shutdown_days_left() -> int:
    """Возвращает количество дней до отключения радио по таймзоне станции."""
    tz = pytz.timezone(TZ_NAME)
    today = datetime.now(tz).date()
    return max((_RADIO_SHUTDOWN_DATE - today).days, 0)


def get_radio_shutdown_date() -> date:
    """Возвращает дату отключения радио."""
    return _RADIO_SHUTDOWN_DATE


def format_radio_shutdown_notice() -> str:
    """Форматирует уведомление о дате отключения радио."""
    days_left = get_radio_shutdown_days_left()
    return (
        f"{_RADIO_SHUTDOWN_EMOJI} Радио отключается *27\\.04\\.26*\\. "
        f"{_RADIO_COUNTDOWN_EMOJI} Осталось *{days_left}* дней\\. {_RADIO_SHUTDOWN_EMOJI}"
    )


def format_radio_farewell_message() -> str:
    """Форматирует финальное MarkdownV2-сообщение на день закрытия радио."""
    return "\n".join([
        "📻 *SBA Radio завершает эфир*",
        "━━━━━━━━━━━━━━━━━━",
        escape_md_v2(
            "Сегодня, 27.04.2026, радио отключается. Спасибо всем, "
            "кто слушал, заказывал треки, голосовал и просто "
            "держал эфир живым."
        ),
        "",
        "*Что успели сделать:*",
        f"• {escape_md_v2('запустили онлайн-поток SBA Radio')}",
        f"• {escape_md_v2('добавили обложки, «сейчас играет», очередь и счетчик слушателей')}",
        f"• {escape_md_v2('сделали заказы треков, голосование за пропуск и поднятие песен')}",
        f"• {escape_md_v2('собрали расписание плейлистов, ежедневные итоги и статистику активности')}",
        f"• {escape_md_v2('подключили TTS-анонсы, мониторинг, логи и healthcheck-и')}",
        f"• {escape_md_v2('отдельно подняли бота для скачивания видео из TikTok, YouTube Shorts и Facebook')}",
        "",
        "*Что останется работать:*",
        f"• {escape_md_v2('бот-загрузчик видео продолжит принимать ссылки и отдавать ролики')}",
        f"• {escape_md_v2('мониторинг и логи останутся для живых сервисов')}",
        "",
        escape_md_v2(
            "Спасибо за этот эфир. Это было сделано чтобы показать насколько свое радио это хорошо!"
        ),
    ])


# --- КЛАВИАТУРА ---

def get_keyboard(listeners: int, song_id: str = None) -> InlineKeyboardMarkup:
    votes, required = get_skip_progress(listeners)
    btn_listen = InlineKeyboardButton("🎧 Слушать", url=STREAM_URL)
    btn_skip = InlineKeyboardButton(f"⏭ Пропустить ({votes}/{required})", callback_data="vote_skip")
    btn_request = InlineKeyboardButton("📝 Заказать трек", url=REQUEST_URL)

    rows = [[btn_listen], [btn_skip, btn_request]]

    if song_id:
        if is_song_in_best(song_id):
            btn_raise = InlineKeyboardButton("✅ Уже в лучших", callback_data="raise_already_best")
        else:
            count = get_song_votes(song_id)
            btn_raise = InlineKeyboardButton(
                f"⬆️ Поднять ({count}/{UPVOTE_THRESHOLD})",
                callback_data="vote_raise",
            )
        rows.append([btn_raise])

    return InlineKeyboardMarkup(rows)


# --- ФОРМАТИРОВАНИЕ СООБЩЕНИЙ ---

def format_main_message(data: dict) -> tuple[str, str, int, str]:
    """Сообщение 1: Сейчас играет. Возвращает (текст, url_обложки, слушатели, song_id)."""
    np = data['now_playing']
    song = np['song']
    listeners = data['listeners']['total']
    song_id = song.get('id', song.get('text'))

    elapsed = np.get('elapsed', 0)
    duration = np.get('duration', 0)
    playlist_raw = np.get('playlist') or 'General'
    playlist_text = str(playlist_raw)
    playlist = escape_md_v2(PLAYLIST_NAMES.get(playlist_text.lower(), playlist_text))

    is_request = np.get('is_request') or str(playlist_raw).lower() == 'requested'
    req_mark = "🎷 *Заказ\\!* " if is_request else ""

    raw_title = song.get('text', 'Unknown')
    raw_artist = song.get('artist', '')
    raw_track = song.get('title', '')
    full_title_md = escape_md_v2(clean_track_info(raw_artist, raw_track, raw_title))

    art_url = song.get('art', '')
    progress_bar = create_progress_bar(elapsed, duration)
    tz = pytz.timezone(TZ_NAME)
    listeners_md = escape_md_v2(str(listeners))
    updated_md = escape_md_v2(datetime.now(tz).strftime('%H:%M:%S'))

    text = (
        f"📻 *SBA Radio Live*\n"
        f"{format_radio_shutdown_notice()}\n"
        f"―――――――\n\n"
        f"🎶 *Сейчас играет:*\n{req_mark}{full_title_md}\n"
        f"{progress_bar}\n\n"
        f"📂 *Плейлист:* {playlist}\n"
        f"👥 *Слушают:* {listeners_md}\n"
        f"🕒 *Обновлено:* {updated_md}\n\n"
        f"―――――――\n"
        f"📋 `/votes view` — голоса • `/votes create` — голосовать"
    )
    return text, art_url, listeners, song_id


def _get_active_block_items(schedule: list) -> list:
    """Returns all currently active schedule items."""
    if not schedule:
        return []
    now_ts = time_module.time()
    return [
        item for item in schedule
        if item.get('is_now') or (item.get('start_timestamp', 0) <= now_ts <= item.get('end_timestamp', 0))
    ]


def _group_schedule_items(items: list) -> list[list]:
    """Группирует элементы расписания по одинаковому интервалу (с точностью до минуты)."""
    groups: dict[tuple[int, int], list] = {}
    for item in items:
        key = (
            item.get('start_timestamp', 0) // 60,
            item.get('end_timestamp', 0) // 60,
        )
        groups.setdefault(key, []).append(item)
    return list(groups.values())


def _format_schedule_item_name(item: dict) -> str:
    raw_name = str(item.get('name') or item.get('title') or '')
    return escape_md(PLAYLIST_NAMES.get(raw_name.lower(), raw_name))


def format_schedule_items_summary(items: list, range_mode: str = 'until') -> str:
    """
    Форматирует список элементов расписания с учётом разных временных диапазонов.

    `range_mode='until'`  -> "Общие _(до 23:59)_"
    `range_mode='range'`  -> "Общие _(00:00 – 23:59)_"
    """
    if not items:
        return ""

    now_ts = int(time_module.time())
    parts: list[str] = []

    for group_items in _group_schedule_items(items):
        first = group_items[0]
        start_ts = first.get('start_timestamp', 0)
        end_ts = first.get('end_timestamp', 0)
        names = ", ".join(_format_schedule_item_name(item) for item in group_items)

        if range_mode == 'range' and start_ts and end_ts:
            if _sched_same_day(now_ts, start_ts):
                time_label = f"{_fmt_sched_time(start_ts)} – {_fmt_sched_time(end_ts)}"
            else:
                time_label = f"{_fmt_sched_date_time(start_ts)} – {_fmt_sched_time(end_ts)}"
        elif end_ts:
            time_label = f"до {_fmt_sched_time(end_ts)}"
        else:
            time_label = "по расписанию"

        parts.append(f"{names} _({time_label})_")

    return "; ".join(parts)


def format_active_schedule_summary(schedule: list) -> str:
    """Возвращает краткую сводку по активным блокам расписания."""
    return format_schedule_items_summary(_get_active_block_items(schedule), range_mode='until')


def format_queue_list(queue_data: list, schedule: list | None = None) -> str:
    """Сообщение 2: Далее в эфире."""
    # Build schedule block header
    sched_prefix = ""
    if schedule:
        active_summary = format_active_schedule_summary(schedule)
        if active_summary:
            sched_prefix = f"📅 *Сейчас:* {active_summary}\n"

    if not queue_data or not isinstance(queue_data, list):
        return sched_prefix + "📂 **Очередь воспроизведения пуста.**"

    lines = []
    count = 0
    current_ts = time_module.time()

    for item in queue_data:
        if count >= 5:
            break

        song = item.get('song', {})
        if isinstance(song, str):
            raw_text, raw_artist, raw_title = song.strip(), "", ""
        else:
            raw_text = song.get('text', '').strip()
            raw_artist = song.get('artist', '').strip()
            raw_title = song.get('title', '').strip()

        if any(ignored in raw_text.lower() for ignored in IGNORED_KEYWORDS):
            continue

        clean_text = clean_track_info(raw_artist, raw_title, raw_text)
        text_md = escape_md(clean_text)

        playlist_raw = item.get('playlist') or ''
        playlist_text = str(playlist_raw)
        playlist_md = escape_md(PLAYLIST_NAMES.get(playlist_text.lower(), playlist_text))

        played_at = item.get('played_at', 0)
        duration = item.get('duration', 0)
        is_request = item.get('is_request', False) or str(playlist_raw).lower() == 'requested'

        starts_in_min = 0
        if played_at > 0:
            starts_in_sec = played_at - current_ts
            if starts_in_sec > 0:
                starts_in_min = int(starts_in_sec // 60)

        infos = []
        if played_at > 0:
            infos.append(f"⏳ {starts_in_min} мин")
        if duration > 0:
            infos.append(f"⏱ {format_duration(duration)}")
        if playlist_md:
            infos.append(f"📂 {playlist_md}")

        info_str = " | ".join(infos)
        req_icon = "🎷 " if is_request else ""
        lines.append(f"{count + 1}. {req_icon}**{text_md}**\n   {info_str}")
        count += 1

    if not lines:
        return sched_prefix + "📂 **Далее в эфире:**\n_(Только служебные джинглы)_"

    header = "🔜 **Далее в эфире:**\n━━━━━━━━━━━━━━━━━━\n"
    return sched_prefix + header + "\n".join(lines)


def _fmt_total_duration(total_seconds: float) -> str:
    h = int(total_seconds // 3600)
    m = int((total_seconds % 3600) // 60)
    s_rem = int(total_seconds % 60)
    if h > 0:
        return f"{h}ч {m}мин {s_rem}сек"
    return f"{m}мин {s_rem}сек"


def format_playlist_announcement(playlist_info: dict, songs: list) -> list[str]:
    """
    Форматирует анонс нового плейлиста, сгруппированный по артистам.
    Возвращает список сообщений (разбивает, если превышен лимит Telegram 4096 символов).
    """
    name = escape_md(playlist_info.get('name', 'Без названия'))
    num_songs = len(songs)
    total_seconds = sum(s.get('length', 0) for s in songs)

    header = (
        f"🎵 *Новый плейлист добавлен!*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📂 *Название:* {name}\n"
        f"🎶 *Треков:* {num_songs}\n"
        f"⏱ *Общее время:* {_fmt_total_duration(total_seconds)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )

    # Group songs by artist
    from collections import defaultdict
    by_artist: dict[str, list] = defaultdict(list)
    for song in songs:
        artist = song.get('artist', '').strip() or 'Неизвестный артист'
        by_artist[artist].append(song)

    # Sort artists alphabetically; unknown last
    def artist_sort_key(a: str) -> tuple:
        return (a == 'Неизвестный артист', a.lower())

    sorted_artists = sorted(by_artist.keys(), key=artist_sort_key)

    # Build lines per artist block
    artist_blocks = []
    for artist in sorted_artists:
        artist_songs = by_artist[artist]
        artist_secs = sum(s.get('length', 0) for s in artist_songs)
        count = len(artist_songs)
        word = 'трек' if count == 1 else ('трека' if 2 <= count <= 4 else 'треков')

        block = f"\n🎤 *{escape_md(artist)}* ({count} {word} | {_fmt_total_duration(artist_secs)})\n"
        for song in artist_songs:
            raw_title = song.get('title', '').strip()
            raw_text = song.get('text', '').strip()
            title = escape_md(raw_title or raw_text or '—')
            dur_str = format_duration(song.get('length', 0))
            block += f"  • {title} `{dur_str}`\n"
        artist_blocks.append(block)

    # Split into messages if needed (Telegram limit ~4096 chars)
    messages = []
    current = header
    for block in artist_blocks:
        if len(current) + len(block) > 4000:
            messages.append(current)
            current = block
        else:
            current += block
    if current.strip():
        messages.append(current)

    return messages if messages else [header]


def _format_changelog_section(text: str) -> str:
    """Formats a comma- or newline-separated list as Markdown bullet points."""
    if not text or not text.strip():
        return "_нет_"
    items = [item.strip() for item in text.replace('\n', ',').split(',') if item.strip()]
    if not items:
        return "_нет_"
    return "\n".join(f"• {escape_md(item)}" for item in items)


def format_changelog(version: str, additions: str, changes: str, deletions: str, notes: str = "") -> str:
    """Formats the full changelog as a single Telegram message."""
    parts = [
        f"📋 *{escape_md(version)}*",
        f"➕ *Добавлено:*\n{_format_changelog_section(additions)}",
        f"✏️ *Изменено:*\n{_format_changelog_section(changes)}",
        f"🗑 *Удалено:*\n{_format_changelog_section(deletions)}",
    ]
    if notes and notes.strip():
        parts.append(f"📝 *Примечания:*\n{escape_md(notes)}")
    return "\n\n".join(parts)


# --- СТАТИСТИКА И ГОЛОСА ---

def format_votes_message(filter_mode: str = 'all', search: str = '') -> str:
    """Форматирует список голосований за треки с фильтрацией и поиском."""
    votes_data = get_all_votes_data()

    if filter_mode == 'added':
        votes_data = [v for v in votes_data if v['in_best']]
    elif filter_mode == 'notadded':
        votes_data = [v for v in votes_data if not v['in_best']]

    if search:
        search_lower = search.lower()
        votes_data = [v for v in votes_data if search_lower in v['title'].lower()]

    if not votes_data:
        return "💭 Нет голосов по заданным фильтрам."

    header = "⬆️ *Голоса за треки*"
    if filter_mode == 'added':
        header += " (добавленные)"
    elif filter_mode == 'notadded':
        header += " (не добавленные)"
    if search:
        header += f" \u00b7 `{escape_md(search)}`"

    separator = "━" * 18
    base = "\n".join([header, separator])
    entries = []
    for v in votes_data:
        title = escape_md(v['title'])
        status = "✅" if v['in_best'] else "⏳"
        entries.append(f"{status} *{title}*\n   — 👍 {v['count']} голосов")

    # Build message respecting Telegram's 4096-char limit (reserve ~100 for timer suffix)
    MAX_LEN = 3900
    result = base
    shown = 0
    for entry in entries:
        candidate = result + "\n" + entry
        if len(candidate) > MAX_LEN:
            remaining = len(entries) - shown
            result += f"\n\n_...и ещё {remaining} треков (используйте фильтры для уточнения)_"
            break
        result = candidate
        shown += 1

    return result


def format_intervals_text(intervals: list) -> str:
    if not intervals:
        return "\n😴 Активности не было\\."
    text = "\n⏱ *Активность по времени:*\n"
    for idx, i in enumerate(intervals, 1):
        dur = i['end_ts'] - i['start_ts']
        duration_text = escape_md_v2(analytics_engine.format_duration(dur))
        text += (
            f"{idx}️⃣ `{i['start']} — {i['end']}` "
            f"\\({duration_text}\\) \\| 👥 {i['max']}\n"
        )
    return text


# --- РАСПИСАНИЕ ---

_MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _fmt_sched_time(ts: int) -> str:
    """Форматирует UNIX-timestamp в HH:MM по таймзоне станции."""
    tz = pytz.timezone(TZ_NAME)
    return datetime.fromtimestamp(ts, tz).strftime('%H:%M')


def _fmt_sched_date_time(ts: int) -> str:
    """Форматирует UNIX-timestamp в 'D месяца, HH:MM' по таймзоне станции."""
    tz = pytz.timezone(TZ_NAME)
    dt = datetime.fromtimestamp(ts, tz)
    return f"{dt.day} {_MONTHS_RU[dt.month]}, {dt.strftime('%H:%M')}"


def _sched_same_day(ts1: int, ts2: int) -> bool:
    """True, если оба timestamp приходятся на один календарный день (по TZ станции)."""
    tz = pytz.timezone(TZ_NAME)
    return (
        datetime.fromtimestamp(ts1, tz).date()
        == datetime.fromtimestamp(ts2, tz).date()
    )


def _format_daily_schedule_range(start_ts: int, end_ts: int, target_date: date) -> str:
    """Форматирует диапазон для суточного расписания с учётом перехода через полночь."""
    tz = pytz.timezone(TZ_NAME)
    day_start = tz.localize(datetime.combine(target_date, datetime.min.time()))
    next_day = day_start + timedelta(days=1)

    clipped_start_ts = max(start_ts, int(day_start.timestamp()))
    start_label = _fmt_sched_time(clipped_start_ts)

    end_dt = datetime.fromtimestamp(end_ts, tz)
    if end_dt < next_day:
        return f"{start_label} – {_fmt_sched_time(end_ts)}"

    if end_dt.date() == next_day.date():
        return f"{start_label} – {_fmt_sched_time(end_ts)} (след. день)"

    return f"{start_label} – {end_dt.strftime('%d.%m %H:%M')}"


def format_daily_schedule_message(schedule: list, target_date: date | None = None) -> str:
    """Форматирует единое MarkdownV2-сообщение с расписанием плейлистов на текущий день."""
    tz = pytz.timezone(TZ_NAME)
    target_date = target_date or datetime.now(tz).date()
    day_start = tz.localize(datetime.combine(target_date, datetime.min.time()))
    next_day = day_start + timedelta(days=1)
    day_start_ts = int(day_start.timestamp())
    next_day_ts = int(next_day.timestamp())
    now_ts = int(datetime.now(tz).timestamp())

    grouped: dict[tuple[int, int], list[dict]] = {}
    for item in sorted(
        schedule or [],
        key=lambda x: (x.get('start_timestamp', 0), x.get('end_timestamp', 0), x.get('id', 0)),
    ):
        if not isinstance(item, dict):
            continue

        start_ts = int(item.get('start_timestamp', 0) or 0)
        end_ts = int(item.get('end_timestamp', 0) or 0)
        if not start_ts or not end_ts:
            continue

        overlaps_day = start_ts < next_day_ts and end_ts > day_start_ts
        if not overlaps_day:
            continue

        grouped.setdefault((start_ts, end_ts), []).append(item)

    date_label = target_date.strftime('%d.%m.%Y')
    header = f"📅 *Расписание плейлистов на {escape_md_v2(date_label)}*\n━━━━━━━━━━━━━━━━━━"

    if not grouped:
        return header + "\n" + escape_md_v2("Сегодня запланированных плейлистов нет.")

    full_day_names: list[str] = []
    timed_groups: list[tuple[tuple[int, int], list[dict]]] = []
    full_day_threshold_ts = next_day_ts - 60

    for time_range, items in grouped.items():
        start_ts, end_ts = time_range
        if start_ts <= day_start_ts and end_ts >= full_day_threshold_ts:
            for item in items:
                raw_name = str(item.get('name') or item.get('title') or '?')
                readable = PLAYLIST_NAMES.get(raw_name.lower(), raw_name)
                if readable not in full_day_names:
                    full_day_names.append(readable)
            continue
        timed_groups.append((time_range, items))

    lines: list[str] = []
    if full_day_names:
        prefix = f"{_SCHEDULE_CURRENT_EMOJI}"
        lines.append(
            f"{prefix} *Весь день:* {', '.join(escape_md_v2(name) for name in full_day_names)}"
        )

    for idx, ((start_ts, end_ts), items) in enumerate(timed_groups, 1):
        names: list[str] = []
        seen_names: set[str] = set()
        for item in items:
            raw_name = str(item.get('name') or item.get('title') or '?')
            readable = PLAYLIST_NAMES.get(raw_name.lower(), raw_name)
            if readable not in seen_names:
                seen_names.add(readable)
                names.append(escape_md_v2(readable))

        time_label = _format_daily_schedule_range(start_ts, end_ts, target_date)
        prefix = f"{_SCHEDULE_CURRENT_EMOJI} " if start_ts <= now_ts < end_ts else ""
        lines.append(f"{prefix}*{escape_md_v2(time_label)}* — {', '.join(names)}")

    return header + "\n" + "\n".join(lines)


def format_schedule_started(
    started_items: list,
    ended_items: list | None = None,
) -> str:
    """
    Сообщение о начале блока расписания.
    started_items  — новые активные элементы.
    ended_items    — только что завершившиеся (для совмещённого сообщения).
    """
    if not started_items:
        return ""

    first     = started_items[0]
    start_ts  = first.get('start_timestamp', 0)
    end_ts    = first.get('end_timestamp', 0)
    names     = [
        escape_md(
            PLAYLIST_NAMES.get(
                str(item.get('name') or '').lower(),
                str(item.get('name') or '?'),
            )
        )
        for item in started_items
    ]
    playlists = ", ".join(names)
    time_range = (
        f"{_fmt_sched_time(start_ts)} – {_fmt_sched_time(end_ts)}"
        if start_ts and end_ts else "?"
    )

    if ended_items:
        prev       = ended_items[0]
        prev_start = prev.get('start_timestamp', 0)
        prev_end   = prev.get('end_timestamp', 0)
        prev_range = (
            f"{_fmt_sched_time(prev_start)} – {_fmt_sched_time(prev_end)}"
            if prev_start and prev_end else "предыдущий"
        )
        return (
            f"✅ Блок песен *{prev_range}* закончился\n"
            f"🔄 Сразу начался новый\n"
            f"🕐 *{time_range}*\n"
            f"📋 Плейлисты: {playlists}"
        )
    return (
        f"📅 *Начался блок песен*\n"
        f"🕐 *{time_range}*\n"
        f"📋 Плейлисты: {playlists}"
    )


def format_schedule_ended(
    ended_items: list,
    next_items: list | None = None,
    still_active: list | None = None,
) -> str:
    """Сообщение об окончании блока расписания."""
    if not ended_items:
        return ""

    first     = ended_items[0]
    start_ts  = first.get('start_timestamp', 0)
    end_ts    = first.get('end_timestamp', 0)
    time_range = (
        f"{_fmt_sched_time(start_ts)} – {_fmt_sched_time(end_ts)}"
        if start_ts and end_ts else "?"
    )
    names = ", ".join(
        escape_md(
            PLAYLIST_NAMES.get(
                str(item.get('name') or '').lower(),
                str(item.get('name') or '?'),
            )
        )
        for item in ended_items
    )

    if still_active:
        active_names = ", ".join(
            escape_md(
                PLAYLIST_NAMES.get(
                    str(item.get('name') or '').lower(),
                    str(item.get('name') or '?'),
                )
            )
            for item in still_active
        )
        return (
            f"✅ Блок песен *{time_range}* закончился\n"
            f"📋 Для: {names}\n"
            f"🎵 Еще играют: {active_names}"
        )

    if next_items:
        next_str = format_schedule_items_summary(next_items, range_mode='range')
        return (
            f"✅ Блок песен *{time_range}* закончился\n"
            f"📋 Для: {names}\n"
            f"⏭ Следующий блок песен: {next_str}"
        )
    return (
        f"✅ Блок песен *{time_range}* закончился\n"
        f"📋 Для: {names}\n"
        f"⏭ Следующих запланированных блоков песен нет"
    )
