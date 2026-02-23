"""
Фоновые задачи бота: обновление плеера и ежедневный отчет.
"""
import logging

from telegram import InputMediaPhoto
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import ContextTypes

from analytics import engine as analytics_engine
from bot.api import get_queue_data, get_station_data
from bot.formatters import (
    format_intervals_text,
    format_main_message,
    format_queue_list,
    get_keyboard,
)
from bot.state import (
    CHATS_DB,
    LAST_MSG_STATE,
    VOTE_STATE,
    save_chats,
    update_vote_logic,
)


async def update_display_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодически обновляет плеер и очередь во всех активных чатах."""
    data = get_station_data()
    if not data:
        return
    queue = get_queue_data()

    main_text, art, listeners, song_id = format_main_message(data)
    queue_text = format_queue_list(queue)

    update_vote_logic(song_id)
    analytics_engine.log_listener_count(listeners)
    kb = get_keyboard(listeners)
    current_kb_hash = str(listeners) + str(len(VOTE_STATE['voters']))

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
