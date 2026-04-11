"""
Фоновые задачи бота: обновление плеера, ежедневный отчет и уведомления расписания.
"""
import json
import logging
import time as _time_module
from datetime import date, datetime, time as dt_time, timedelta

import pytz
from telegram import InputMediaPhoto
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import ContextTypes

from analytics import engine as analytics_engine
from bot.api import get_queue_data, get_schedule, get_station_data
from bot.formatters import (
    clean_track_info,
    escape_md_v2,
    format_daily_schedule_message,
    format_intervals_text,
    format_main_message,
    format_queue_list,
    format_radio_shutdown_notice,
    get_keyboard,
)
from bot.state import (
    CHATS_DB,
    LAST_MSG_STATE,
    VOTE_STATE,
    add_recent_song,
    get_song_votes,
    load_schedule_notify_state,
    save_chats,
    save_schedule_notify_state,
    update_vote_logic,
)
from core.config import TZ_NAME

# --- Состояние джобы расписания ---
_sched_state = load_schedule_notify_state()
_sched_last_date: str = str(_sched_state.get('date') or '')
_sched_last_signature: str = str(_sched_state.get('signature') or '')
_sched_last_text: str = str(_sched_state.get('text') or '')
_sched_daily_messages: dict[str, int] = {
    str(chat_id): int(message_id)
    for chat_id, message_id in dict(_sched_state.get('messages') or {}).items()
}


def _today_schedule_key() -> str:
    tz = pytz.timezone(TZ_NAME)
    return datetime.now(tz).strftime('%Y-%m-%d')


def _persist_sched_state() -> None:
    save_schedule_notify_state({
        'date': _sched_last_date,
        'signature': _sched_last_signature,
        'text': _sched_last_text,
        'messages': _sched_daily_messages,
    })


def _build_daily_schedule_signature(schedule: list, target_date: date) -> str:
    """Строит стабильную сигнатуру дневного расписания без учёта текущего времени."""
    tz = pytz.timezone(TZ_NAME)
    day_start = tz.localize(datetime.combine(target_date, dt_time.min))
    next_day = day_start + timedelta(days=1)
    day_start_ts = int(day_start.timestamp())
    next_day_ts = int(next_day.timestamp())
    normalized: list[dict[str, object]] = []

    for item in sorted(
        schedule or [],
        key=lambda x: (
            x.get('start_timestamp', 0),
            x.get('end_timestamp', 0),
            x.get('id', 0),
            str(x.get('name') or x.get('title') or ''),
        ),
    ):
        if not isinstance(item, dict):
            continue

        start_ts = int(item.get('start_timestamp', 0) or 0)
        end_ts = int(item.get('end_timestamp', 0) or 0)
        if not start_ts or not end_ts:
            continue

        if not (start_ts < next_day_ts and end_ts > day_start_ts):
            continue

        normalized.append({
            'id': int(item.get('id', 0) or 0),
            'start': start_ts,
            'end': end_ts,
            'name': str(item.get('name') or item.get('title') or '?'),
        })

    return json.dumps(normalized, ensure_ascii=False, separators=(',', ':'))


async def update_display_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодически обновляет плеер и очередь во всех активных чатах."""
    data = await get_station_data()
    if not data:
        return
    queue = await get_queue_data()
    schedule = await get_schedule(rows=24)

    main_text, art, listeners, song_id = format_main_message(data)
    queue_text = format_queue_list(queue, schedule=schedule)

    song_data = data['now_playing']['song']
    display_title = clean_track_info(
        song_data.get('artist', ''),
        song_data.get('title', ''),
        song_data.get('text', ''),
    )
    changed = update_vote_logic(song_id)
    if changed:
        add_recent_song(
            song_id,
            display_title=display_title,
            artist=song_data.get('artist', ''),
            title=song_data.get('title', ''),
        )
    analytics_engine.log_listener_count(listeners)
    kb = get_keyboard(listeners, song_id)
    current_kb_hash = str(listeners) + str(len(VOTE_STATE['voters'])) + str(get_song_votes(song_id))

    chats_to_remove = []

    for chat_id, msg_ids in list(CHATS_DB.items()):
        if not isinstance(msg_ids, dict):
            chats_to_remove.append(chat_id)
            continue

        main_id = msg_ids.get('main')
        queue_id = msg_ids.get('queue')
        last_state = LAST_MSG_STATE.get(chat_id, {})

        # 1. Обновление плеера
        if main_id:
            text_changed = last_state.get('main_text') != main_text
            kb_changed = last_state.get('kb_hash') != current_kb_hash

            try:
                if text_changed:
                    await context.bot.edit_message_media(
                        chat_id=chat_id,
                        message_id=main_id,
                        media=InputMediaPhoto(media=art, caption=main_text, parse_mode='MarkdownV2'),
                        reply_markup=kb,
                    )
                elif kb_changed:
                    await context.bot.edit_message_reply_markup(
                        chat_id=chat_id, message_id=main_id, reply_markup=kb
                    )
            except (TimedOut, NetworkError):
                logging.warning(f"Timeout main msg {chat_id}")
            except BadRequest as e:
                if "not found" in str(e):
                    chats_to_remove.append(chat_id)
            except Exception:
                pass

        # 2. Обновление очереди
        if queue_id and chat_id not in chats_to_remove:
            if last_state.get('queue_text') != queue_text:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=queue_id,
                        text=queue_text,
                        parse_mode='Markdown',
                    )
                except (TimedOut, NetworkError):
                    logging.warning(f"Timeout queue msg {chat_id}")
                except (BadRequest, Exception):
                    pass

        LAST_MSG_STATE[chat_id] = {
            'main_text': main_text,
            'queue_text': queue_text,
            'kb_hash': current_kb_hash,
        }

    if chats_to_remove:
        for cid in chats_to_remove:
            CHATS_DB.pop(cid, None)
            LAST_MSG_STATE.pop(cid, None)
        save_chats(CHATS_DB)


async def schedule_notify_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет единое сообщение с расписанием плейлистов на текущий день.
    В полночь создаёт новое сообщение, а после рестарта восстанавливает состояние
    и досылает сообщение только если за сегодня его ещё нет.
    """
    global _sched_last_date, _sched_last_signature, _sched_last_text, _sched_daily_messages

    if not CHATS_DB:
        return

    today_key = _today_schedule_key()
    active_chat_keys = {str(chat_id) for chat_id in CHATS_DB.keys()}

    filtered_messages = {
        chat_id: message_id
        for chat_id, message_id in _sched_daily_messages.items()
        if chat_id in active_chat_keys and message_id
    }
    if filtered_messages != _sched_daily_messages:
        _sched_daily_messages = filtered_messages
        _persist_sched_state()

    schedule = await get_schedule(rows=200)
    if not schedule or not isinstance(schedule, list):
        return

    tz = pytz.timezone(TZ_NAME)
    target_date = datetime.now(tz).date()
    schedule_signature = _build_daily_schedule_signature(schedule, target_date)
    schedule_message = format_daily_schedule_message(schedule, target_date=target_date)

    schedule_changed = (
        _sched_last_date != today_key
        or _sched_last_signature != schedule_signature
    )
    missing_chat_keys = set(active_chat_keys) - set(_sched_daily_messages.keys())

    if schedule_changed:
        missing_chat_keys = set(active_chat_keys)
        _sched_daily_messages = {}
    elif not missing_chat_keys:
        return

    for chat_id in list(CHATS_DB.keys()):
        chat_key = str(chat_id)
        if chat_key not in missing_chat_keys:
            continue

        try:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=schedule_message,
                parse_mode='MarkdownV2',
            )
            _sched_daily_messages[chat_key] = sent.message_id
        except Exception as exc:
            logging.warning(f"Schedule notify -> {chat_id}: {exc}")

    _sched_last_date = today_key
    _sched_last_signature = schedule_signature
    _sched_last_text = schedule_message
    _persist_sched_state()


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """В полночь отправляет итоги дня во все активные чаты."""
    logging.info("Starting daily report job...")

    if not CHATS_DB:
        logging.warning("Daily report aborted: No active chats found.")
        return

    stats = analytics_engine.rotate_daily_logs()

    shutdown_notice = format_radio_shutdown_notice()

    if not stats:
        msg = (
            "🏁 *Итоги дня*\n"
            "━━━━━━━━━━\n"
            "🤷‍♂️ Данных о слушателях не было\\.\n"
            f"{shutdown_notice}"
        )
    else:
        intervals = format_intervals_text(stats['intervals']).rstrip()
        trend = stats['change_percent']
        emoji = "📈" if trend >= 0 else "📉"
        date_md = stats['date'].replace('-', '\\-')
        max_md = str(stats['max'])
        avg_md = escape_md_v2(f"{stats['avg']:.1f}")
        trend_md = escape_md_v2(f"{trend:+.1f}%")
        msg = (
            f"🏁 *Итоги дня* \\({date_md}\\)\n━━━━━━━━━━\n"
            f"👥 Пик: *{max_md}*\n"
            f"📊 Среднее: *{avg_md}*\n"
            f"{emoji} Динамика: *{trend_md}*\n"
            f"━━━━━━━━━━{intervals}\n"
            f"{shutdown_notice}"
        )

    for chat_id in list(CHATS_DB.keys()):
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='MarkdownV2')
            logging.info(f"Report sent to {chat_id}")
        except Exception as e:
            logging.error(f"Failed to send report to {chat_id}: {e}")
