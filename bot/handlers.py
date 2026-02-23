"""
Обработчики команд и callback-кнопок Telegram-бота.
"""
import logging

from telegram import Update
from telegram.error import TimedOut
from telegram.ext import ContextTypes

from analytics import engine as analytics_engine
from bot.api import get_playlist_info, get_playlist_songs, get_queue_data, get_station_data, skip_song_api
from bot.formatters import (
    format_intervals_text,
    format_main_message,
    format_playlist_announcement,
    format_queue_list,
    get_keyboard,
)
from bot.state import (
    CHATS_DB,
    LAST_MSG_STATE,
    VOTE_STATE,
    get_skip_progress,
    save_chats,
    update_vote_logic,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start — отправляет плеер и очередь в чат."""
    chat_id = str(update.effective_chat.id)

    # Удаляем старые сообщения
    if chat_id in CHATS_DB:
        ids = CHATS_DB[chat_id]
        for key in ('main', 'queue'):
            if key in ids:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=ids[key])
                except Exception:
                    pass
        del CHATS_DB[chat_id]
        LAST_MSG_STATE.pop(chat_id, None)

    data = get_station_data()
    queue = get_queue_data()

    if not data:
        await update.message.reply_text("⚠️ API недоступно.")
        return

    main_text, art, listeners, song_id = format_main_message(data)
    update_vote_logic(song_id)
    kb = get_keyboard(listeners)
    queue_text = format_queue_list(queue)

    try:
        msg_main = await update.message.reply_photo(
            photo=art, caption=main_text, reply_markup=kb, parse_mode='Markdown'
        )
        msg_queue = await update.message.reply_text(text=queue_text, parse_mode='Markdown')

        CHATS_DB[chat_id] = {'main': msg_main.message_id, 'queue': msg_queue.message_id}
        save_chats(CHATS_DB)

        LAST_MSG_STATE[chat_id] = {
            'main_text': main_text,
            'queue_text': queue_text,
            'kb_hash': str(listeners) + str(len(VOTE_STATE['voters'])),
        }

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except Exception:
            pass

    except TimedOut:
        await update.message.reply_text("⚠️ Сервер Telegram перегружен. Повторите /start позже.")
    except Exception as e:
        logging.error(f"Error in start: {e}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатие кнопки «Пропустить»."""
    query = update.callback_query
    if query.data != "vote_skip":
        return

    user_id = query.from_user.id
    data = get_station_data()
    if not data:
        return

    listeners = data['listeners']['total']
    votes, required = get_skip_progress(listeners)

    if user_id in VOTE_STATE['voters']:
        await query.answer("Уже голосовали!", show_alert=True)
        return

    VOTE_STATE['voters'].add(user_id)
    votes += 1
    await query.answer(f"Голос принят! ({votes}/{required})")

    if votes >= required:
        success, msg = skip_song_api()
        msg_text = "✅ Пропускаем!" if success else f"Ошибка: {msg}"
        await query.answer(msg_text, show_alert=True)

    try:
        await query.edit_message_reply_markup(reply_markup=get_keyboard(listeners))
    except Exception:
        pass


async def announcement_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /announcement playlist <id> — отправляет анонс плейлиста в чат.
    Сообщение отправителя удаляется.
    """
    args = context.args

    # Delete the sender's command message immediately
    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
        )
    except Exception:
        pass

    if not args or len(args) < 2 or args[0].lower() != 'playlist':
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Использование: /announcement playlist <id>\nПример: /announcement playlist 12",
        )
        return

    try:
        playlist_id = int(args[1])
    except ValueError:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ ID плейлиста должен быть числом. Пример: /announcement playlist 12",
        )
        return

    loading_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ Загружаю информацию о плейлисте...",
    )

    playlist_info = get_playlist_info(playlist_id)
    if not playlist_info:
        await loading_msg.edit_text(f"❌ Плейлист с ID {playlist_id} не найден или API недоступен.")
        return

    songs = get_playlist_songs(playlist_id)
    messages = format_playlist_announcement(playlist_info, songs)

    # Replace loading message with first part, send the rest
    try:
        await loading_msg.edit_text(messages[0], parse_mode='Markdown')
    except Exception as e:
        logging.error(f"Error editing loading message: {e}")
        await loading_msg.delete()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=messages[0],
            parse_mode='Markdown',
        )

    for extra_msg in messages[1:]:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=extra_msg,
                parse_mode='Markdown',
            )
        except Exception as e:
            logging.error(f"Error sending announcement part: {e}")


async def test_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /testreport — принудительно генерирует дневной отчет (без очистки)."""
    await update.message.reply_text("⏳ Генерирую отчет...")
    stats = analytics_engine.get_today_report_data()

    if not stats:
        await update.message.reply_text("⚠️ Файл статистики пуст. Запустите радио и подождите 30 сек.")
        return

    intervals = format_intervals_text(stats['intervals'])
    trend = stats['change_percent']
    emoji = "📈" if trend >= 0 else "📉"

    msg = (
        f"🧪 *Тестовый отчет* (текущий день)\n━━━━━━━━━━\n"
        f"👥 Пик: *{stats['max']}*\n"
        f"📊 Среднее: *{stats['avg']:.1f}*\n"
        f"{emoji} Динамика: *{trend:+.1f}%*\n"
        f"━━━━━━━━━━{intervals}"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')
