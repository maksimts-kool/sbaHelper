"""
Фоновые задачи бота: обновление плеера, ежедневный отчет и уведомления расписания.
"""
import logging
import time as _time_module

from telegram import InputMediaPhoto
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import ContextTypes

from analytics import engine as analytics_engine
from bot.api import get_queue_data, get_schedule, get_station_data
from bot.formatters import (
    clean_track_info,
    format_intervals_text,
    format_main_message,
    format_queue_list,
    format_schedule_ended,
    format_schedule_started,
    get_keyboard,
)
from bot.state import (
    CHATS_DB,
    LAST_MSG_STATE,
    VOTE_STATE,
    add_recent_song,
    get_song_votes,
    save_chats,
    update_vote_logic,
)

# --- Состояние джобы расписания (хранится в памяти процесса) ---
# Ключ: (playlist_id, start_timestamp) — уникальный слот расписания.
# Простой id не подходит: один плейлист может повторяться в разные дни с тем же id.
_sched_active_keys: set[tuple[int, int]] = set()
_sched_active_data: dict[tuple[int, int], dict] = {}
_sched_initialized: bool = False
# Последнее уведомление расписания по chat_id: {chat_id: message_id}
_sched_last_msg: dict[int, int] = {}


def _sched_key(item: dict) -> tuple[int, int]:
    return (item.get('id', 0), item.get('start_timestamp', 0))


async def update_display_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодически обновляет плеер и очередь во всех активных чатах."""
    data = get_station_data()
    if not data:
        return
    queue = get_queue_data()

    main_text, art, listeners, song_id = format_main_message(data)
    queue_text = format_queue_list(queue)

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
                        media=InputMediaPhoto(media=art, caption=main_text, parse_mode='Markdown'),
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
    Каждую минуту сверяет расписание AzuraCast и отправляет уведомления:
    — когда начинается запланированный блок;
    — когда он заканчивается (с информацией о следующем);
    — совмещённое сообщение при немедленном переходе.
    """
    global _sched_active_keys, _sched_active_data, _sched_initialized

    if not CHATS_DB:
        return

    schedule = get_schedule(rows=48)
    if not schedule or not isinstance(schedule, list):
        return

    now_ts = int(_time_module.time())

    # Элементы, активные прямо сейчас (ключ — уникальный слот, а не просто playlist id)
    active_items: dict[tuple[int, int], dict] = {
        _sched_key(item): item
        for item in schedule
        if isinstance(item, dict) and item.get('is_now')
    }
    current_active_keys = set(active_items.keys())

    # Ближайшие ещё не начавшиеся элементы
    upcoming = sorted(
        [
            item for item in schedule
            if isinstance(item, dict)
            and not item.get('is_now')
            and item.get('start_timestamp', 0) > now_ts
        ],
        key=lambda x: x.get('start_timestamp', 0),
    )

    # Все элементы ближайшего блока (одинаковый start_timestamp)
    next_items: list = []
    if upcoming:
        nearest_start = upcoming[0].get('start_timestamp', 0)
        next_items = [
            item for item in upcoming
            if item.get('start_timestamp', 0) == nearest_start
        ]

    if not _sched_initialized:
        _sched_active_keys  = current_active_keys
        _sched_active_data  = active_items
        _sched_initialized  = True
        # Если уже что-то играет — уведомляем как о только что начавшемся блоке
        if current_active_keys:
            groups: dict[tuple[int, int], list] = {}
            for item in active_items.values():
                gkey = (
                    item.get('start_timestamp', 0) // 60,
                    item.get('end_timestamp', 0) // 60,
                )
                groups.setdefault(gkey, []).append(item)
            for group_items in groups.values():
                msg = format_schedule_started(group_items)
                if msg:
                    for chat_id in list(CHATS_DB.keys()):
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=msg,
                                parse_mode='Markdown',
                            )
                        except Exception as exc:
                            logging.warning(f"Schedule notify (init) -> {chat_id}: {exc}")
        return

    newly_started_keys = current_active_keys - _sched_active_keys
    just_ended_keys    = _sched_active_keys - current_active_keys

    if not newly_started_keys and not just_ended_keys:
        _sched_active_keys  = current_active_keys
        _sched_active_data  = active_items
        return

    newly_started = [active_items[k] for k in newly_started_keys]
    just_ended    = [
        _sched_active_data[k]
        for k in just_ended_keys
        if k in _sched_active_data
    ]

    _sched_active_keys  = current_active_keys
    _sched_active_data  = active_items

    messages: list[str] = []

    if newly_started:
        # Ищем, не является ли это немедленным переходом
        is_transition = bool(just_ended) and any(
            abs(s.get('start_timestamp', 0) - e.get('end_timestamp', 0)) <= 120
            for s in newly_started
            for e in just_ended
        )

        # Группируем новые элементы по временному диапазону (точность ±1 мин)
        groups: dict[tuple[int, int], list] = {}
        for item in newly_started:
            key = (
                item.get('start_timestamp', 0) // 60,
                item.get('end_timestamp', 0) // 60,
            )
            groups.setdefault(key, []).append(item)

        for group_items in groups.values():
            msg = format_schedule_started(
                group_items,
                just_ended if is_transition else None,
            )
            if msg:
                messages.append(msg)
    elif just_ended:
        # Блок(и) завершились, новых нет.
        # Группируем по (start//60, end//60), чтобы плейлисты с одинаковым временем составляли один блок.
        ended_groups: dict[tuple[int, int], list] = {}
        for item in just_ended:
            ekey = (
                item.get('start_timestamp', 0) // 60,
                item.get('end_timestamp', 0) // 60,
            )
            ended_groups.setdefault(ekey, []).append(item)
        for ended_group in ended_groups.values():
            still = list(active_items.values()) or None
            msg = format_schedule_ended(ended_group, next_items or None, still)
            if msg:
                messages.append(msg)

    for msg in messages:
        for chat_id in list(CHATS_DB.keys()):
            try:
                # Удаляем предыдущее уведомление расписания
                prev_id = _sched_last_msg.get(chat_id)
                if prev_id:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=prev_id)
                    except Exception:
                        pass
                sent = await context.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode='Markdown',
                )
                _sched_last_msg[chat_id] = sent.message_id
            except Exception as exc:
                logging.warning(f"Schedule notify -> {chat_id}: {exc}")


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """В полночь отправляет итоги дня во все активные чаты."""
    logging.info("Starting daily report job...")

    if not CHATS_DB:
        logging.warning("Daily report aborted: No active chats found.")
        return

    stats = analytics_engine.rotate_daily_logs()

    if not stats:
        msg = "🏁 *Итоги дня*\n━━━━━━━━━━\n🤷‍♂️ Данных о слушателях не было."
    else:
        intervals = format_intervals_text(stats['intervals'])
        trend = stats['change_percent']
        emoji = "📈" if trend >= 0 else "📉"
        msg = (
            f"🏁 *Итоги дня* ({stats['date']})\n━━━━━━━━━━\n"
            f"👥 Пик: *{stats['max']}*\n"
            f"📊 Среднее: *{stats['avg']:.1f}*\n"
            f"{emoji} Динамика: *{trend:+.1f}%*\n"
            f"━━━━━━━━━━{intervals}"
        )

    for chat_id in list(CHATS_DB.keys()):
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
            logging.info(f"Report sent to {chat_id}")
        except Exception as e:
            logging.error(f"Failed to send report to {chat_id}: {e}")
