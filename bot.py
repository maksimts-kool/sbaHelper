import os
import logging
import requests
import asyncio
import json
import time as time_module
from datetime import time, datetime, timedelta
import pytz

# Импорты Telegram
from telegram import Update, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler
from telegram.request import HTTPXRequest
from telegram.error import BadRequest, TimedOut, NetworkError

# Импорт аналитики
import analytics_engine

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
AZURACAST_HOST = os.getenv("AZURACAST_HOST")
STATION_ID = os.getenv("STATION_ID")
API_KEY = os.getenv("API_KEY")
TZ_NAME = os.getenv("TZ", "Europe/Tallinn")

# Ссылки
STREAM_URL = "https://radio.maksimtsikvasvili24.thkit.ee/listen/sbaradio/radio.mp3"
REQUEST_URL = "https://radio.maksimtsikvasvili24.thkit.ee/public/sbaradio/embed-requests"

# Список слов для скрытия из очереди
IGNORED_KEYWORDS = ["intro", "tts next5", "tts time announce", "tts_next5", "tts_time"]

API_HEADERS = {"Authorization": f"Bearer {API_KEY}"}
CHATS_FILE = os.path.join("bot_data", "active_chats.json")

# --- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ---
VOTE_STATE = {
    'song_id': None,
    'voters': set()
}

LAST_MSG_STATE = {}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def escape_md(text):
    """Экранирует спецсимволы для MarkdownV2 (или Legacy Markdown)."""
    if not text:
        return ""
    # Для Markdown (обычного) критичны: _ * ` [
    special_chars = ['_', '*', '`', '[', ']']
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text

def load_chats():
    if os.path.exists(CHATS_FILE):
        try:
            with open(CHATS_FILE, 'r') as f:
                data = json.load(f)
                cleaned_data = {}
                for k, v in data.items():
                    if isinstance(v, dict) and 'main' in v:
                        cleaned_data[k] = v
                return cleaned_data
        except Exception as e:
            logging.error(f"Error loading chats file: {e}")
            return {}
    return {}

def save_chats(data):
    try:
        with open(CHATS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logging.error(f"Error saving chats file: {e}")

CHATS_DB = load_chats()

# --- ФУНКЦИИ API AZURACAST ---

def get_station_data():
    """Получает данные о текущем треке."""
    try:
        url = f"{AZURACAST_HOST}/api/nowplaying/{STATION_ID}"
        r = requests.get(url, headers=API_HEADERS, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        logging.error(f"API Error (NowPlaying): {e}")
        return None

def get_queue_data():
    """Получает точный список очереди."""
    try:
        url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/queue"
        r = requests.get(url, headers=API_HEADERS, timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        logging.error(f"API Error (Queue): {e}")
        return []

def skip_song_api():
    try:
        url = f"{AZURACAST_HOST}/api/station/{STATION_ID}/backend/skip"
        r = requests.post(url, headers=API_HEADERS, timeout=10)
        return (True, "Skipped") if r.status_code == 200 else (False, f"Error {r.status_code}")
    except Exception as e:
        return False, str(e)

# --- ЛОГИКА ---

def update_vote_logic(current_song_id):
    global VOTE_STATE
    if VOTE_STATE['song_id'] != current_song_id:
        VOTE_STATE['song_id'] = current_song_id
        VOTE_STATE['voters'] = set()
        return True
    return False

def get_skip_progress(total_listeners):
    if total_listeners <= 1: required = 1
    else: required = int(total_listeners * 0.5)
    return len(VOTE_STATE['voters']), required

# --- ФОРМАТИРОВАНИЕ ---

def format_duration(seconds):
    if not seconds: return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02}:{s:02}"

def get_keyboard(listeners):
    votes, required = get_skip_progress(listeners)
    btn_listen = InlineKeyboardButton("🎧 Слушать", url=STREAM_URL)
    btn_skip = InlineKeyboardButton(f"⏭ Пропустить ({votes}/{required})", callback_data="vote_skip")
    btn_request = InlineKeyboardButton("📝 Заказать трек", url=REQUEST_URL)
    return InlineKeyboardMarkup([[btn_listen], [btn_skip, btn_request]])

def create_progress_bar(elapsed, total, length=10):
    """Генерирует строку вида: 01:20 ━━🔘━━━━━━ 03:45"""
    if total == 0:
        return "" # Если длительность неизвестна (например, прямой эфир без треков)
    
    # Ограничиваем прогресс 100%
    percent = min(elapsed / total, 1.0)
    filled_length = int(length * percent)
    
    # Используем спецсимволы для красоты
    # ━ (толстая черта), 🔘 (курсор), ─ (тонкая черта или другая)
    bar = '━' * filled_length + '🔘' + '─' * (length - filled_length)
    
    time_str = f"`{format_duration(elapsed)} {bar} {format_duration(total)}`"
    return time_str

def format_main_message(data):
    """Сообщение 1: Сейчас играет (с прогресс-баром)"""
    np = data['now_playing']
    song = np['song']
    listeners = data['listeners']['total']
    song_id = song.get('id', song.get('text'))
    
    # Данные о времени
    elapsed = np.get('elapsed', 0)
    duration = np.get('duration', 0)
    
    # ЭКРАНИРОВАНИЕ
    raw_title = song.get('text', 'Unknown')
    raw_artist = song.get('artist', '')
    raw_track = song.get('title', '')
    
    title = escape_md(raw_title)
    artist = escape_md(raw_artist)
    track = escape_md(raw_track)
    
    full_title = f"{artist} - {track}" if (artist and track) else title
    playlist = escape_md(np.get('playlist', 'General'))
    art_url = song.get('art', '')

    # Генерация полосы проигрывания
    progress_bar = create_progress_bar(elapsed, duration)

    text = (
        f"📻 **SBA Radio Live**\n\n"
        f"🎶 **Сейчас играет:**\n{full_title}\n"
        f"{progress_bar}\n\n"  # <-- Вставляем полосу сюда
        f"📂 **Плейлист:** {playlist}\n"
        f"👥 **Слушают:** {listeners}\n"
        f"🕒 **Обновлено:** {datetime.now(pytz.timezone(TZ_NAME)).strftime('%H:%M:%S')}"
    )
    return text, art_url, listeners, song_id

def format_queue_list(queue_data):
    """Формирует сообщение 'Далее в эфире' из списка очереди."""
    if not queue_data or not isinstance(queue_data, list):
        return "📂 **Очередь воспроизведения пуста.**"

    lines = []
    count = 0
    current_ts = time_module.time()

    for item in queue_data:
        if count >= 5: break
        
        # item - это словарь конкретной песни в очереди
        song = item.get('song', {})
        if isinstance(song, str):
             # Редкий случай, если API вернул строку вместо объекта song
            raw_text = song.strip()
        else:
            raw_text = song.get('text', '').strip()

        # Фильтрация
        if any(ignored in raw_text.lower() for ignored in IGNORED_KEYWORDS):
            continue

        text = escape_md(raw_text)
        playlist = escape_md(item.get('playlist', ''))
        
        played_at = item.get('played_at', 0)
        duration = item.get('duration', 0)
        is_request = item.get('is_request', False)

        # Расчет времени до начала
        starts_in_min = 0
        if played_at > 0:
            starts_in_sec = played_at - current_ts
            if starts_in_sec > 0:
                starts_in_min = int(starts_in_sec // 60)
        
        # Сборка строки с информацией
        infos = []
        if played_at > 0:
            infos.append(f"⏳ {starts_in_min} мин")
        if duration > 0:
            infos.append(f"⏱ {format_duration(duration)}")
        if playlist:
            infos.append(f"📂 {playlist}")
        
        info_str = " | ".join(infos)
        req_icon = "🎷 **Заказ!** " if is_request else ""
        
        # Формат: 
        # 1. Artist - Title
        #    ⏳ 5 мин | ⏱ 03:20 | 📂 radio 2025
        lines.append(f"{count + 1}. {req_icon}**{text}**\n   {info_str}")
        count += 1
    
    if not lines:
        return "📂 **Далее в эфире:**\n_(Только служебные джинглы)_"

    header = "🔜 **Далее в эфире:**\n━━━━━━━━━━━━━━━━━━\n"
    return header + "\n".join(lines)

def format_intervals_text(intervals):
    if not intervals: return "\n😴 Активности не было."
    text = "\n⏱ **Активность по времени:**\n"
    for idx, i in enumerate(intervals, 1):
        dur = i['end_ts'] - i['start_ts']
        text += f"{idx}️⃣ `{i['start']} — {i['end']}` ({analytics_engine.format_duration(dur)}) | 👥 {i['max']}\n"
    return text

# --- ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    
    # Очистка старых сообщений
    if chat_id in CHATS_DB:
        ids = CHATS_DB[chat_id]
        if 'main' in ids:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=ids['main'])
            except: pass
        if 'queue' in ids:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=ids['queue'])
            except: pass
        del CHATS_DB[chat_id]
        if chat_id in LAST_MSG_STATE: del LAST_MSG_STATE[chat_id]

    # Запрашиваем данные
    data = get_station_data() # Now Playing
    queue = get_queue_data()  # Queue List

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
        msg_queue = await update.message.reply_text(
            text=queue_text, parse_mode='Markdown'
        )

        CHATS_DB[chat_id] = {'main': msg_main.message_id, 'queue': msg_queue.message_id}
        save_chats(CHATS_DB)

        LAST_MSG_STATE[chat_id] = {
            'main_text': main_text,
            'queue_text': queue_text,
            'kb_hash': str(listeners) + str(len(VOTE_STATE['voters']))
        }

        try: await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)
        except: pass

    except TimedOut:
        await update.message.reply_text("⚠️ Сервер Telegram перегружен. Повторите /start позже.")
    except Exception as e:
        logging.error(f"Error in start: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data != "vote_skip": return
    
    user_id = query.from_user.id
    data = get_station_data()
    if not data: return
    
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
    except: pass

# --- ФОНОВЫЕ ЗАДАЧИ ---

async def update_display_job(context: ContextTypes.DEFAULT_TYPE):
    # Получаем данные из двух источников
    data = get_station_data()
    if not data: return
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

        # 1. ОБНОВЛЕНИЕ ПЛЕЕРА
        if main_id:
            text_changed = last_state.get('main_text') != main_text
            kb_changed = last_state.get('kb_hash') != current_kb_hash
            
            try:
                if text_changed:
                    await context.bot.edit_message_media(
                        chat_id=chat_id, message_id=main_id,
                        media=InputMediaPhoto(media=art, caption=main_text, parse_mode='Markdown'),
                        reply_markup=kb
                    )
                elif kb_changed:
                    await context.bot.edit_message_reply_markup(
                        chat_id=chat_id, message_id=main_id, reply_markup=kb
                    )
            except (TimedOut, NetworkError):
                logging.warning(f"Timeout main msg {chat_id}")
            except BadRequest as e:
                if "not found" in str(e): chats_to_remove.append(chat_id)
            except Exception: pass

        # 2. ОБНОВЛЕНИЕ ОЧЕРЕДИ
        if queue_id and chat_id not in chats_to_remove:
            if last_state.get('queue_text') != queue_text:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=queue_id,
                        text=queue_text, parse_mode='Markdown'
                    )
                except (TimedOut, NetworkError):
                    logging.warning(f"Timeout queue msg {chat_id}")
                except BadRequest: pass
                except Exception: pass

        LAST_MSG_STATE[chat_id] = {
            'main_text': main_text,
            'queue_text': queue_text,
            'kb_hash': current_kb_hash
        }

    if chats_to_remove:
        for cid in chats_to_remove:
            if cid in CHATS_DB: del CHATS_DB[cid]
            if cid in LAST_MSG_STATE: del LAST_MSG_STATE[cid]
        save_chats(CHATS_DB)

async def test_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительная отправка отчета (для тестов)."""
    await update.message.reply_text("⏳ Генерирую отчет...")
    # Используем get_today_report_data, чтобы НЕ очищать статистику при тесте
    stats = analytics_engine.get_today_report_data()
    
    if not stats:
        await update.message.reply_text("⚠️ Файл статистики пуст. Запустите радио и подождите 30 сек.")
        return

    # Формируем текст (тот же код, что в ежедневном отчете)
    intervals = format_intervals_text(stats['intervals'])
    trend = stats['change_percent']
    emoji = "📈" if trend >= 0 else "📉"
    
    msg = (f"🧪 **Тестовый отчет**\n━━━━━━━━━━\n👥 Пик: **{stats['max']}**\n"
           f"📉 Среднее: **{stats['avg']:.1f}**\n{emoji} Динамика: **{trend:+.1f}%**\n━━━━━━━━━━{intervals}")
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    logging.info("Starting daily report job...")
    
    # Если чатов нет, некуда слать
    if not CHATS_DB:
        logging.warning("Daily report aborted: No active chats found.")
        return

    stats = analytics_engine.rotate_daily_logs()
    
    if not stats:
        # Если данных совсем нет, отправляем уведомление об этом, чтобы знать, что бот жив
        msg = "🏁 **Итоги дня**\n━━━━━━━━━━\n🤷‍♂️ Данных о слушателях не было."
    else:
        intervals = format_intervals_text(stats['intervals'])
        trend = stats['change_percent']
        emoji = "📈" if trend >= 0 else "📉"
        msg = (f"🏁 **Итоги дня** ({stats['date']})\n━━━━━━━━━━\n👥 Пик: **{stats['max']}**\n"
               f"📉 Среднее: **{stats['avg']:.1f}**\n{emoji} Динамика: **{trend:+.1f}%**\n━━━━━━━━━━{intervals}")

    for chat_id in CHATS_DB.keys():
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
            logging.info(f"Report sent to {chat_id}")
        except Exception as e:
            logging.error(f"Failed to send report to {chat_id}: {e}")

if __name__ == "__main__":
    if not TOKEN: exit(1)
    
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=30.0,
        write_timeout=30.0,
        connect_timeout=30.0
    )
    
    application = ApplicationBuilder().token(TOKEN).request(request).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("testreport", test_report_command))
    application.add_handler(CallbackQueryHandler(button_callback))

    jq = application.job_queue
    
    # Обновление дисплея каждые 30 сек
    jq.run_repeating(update_display_job, interval=30, first=10, job_kwargs={'misfire_grace_time': 10})
    
    # --- ОТЧЕТ В 00:00 ---
    # Устанавливаем полночь по Таллинну.
    # misfire_grace_time=60 дает боту 1 минуту форы, если в 00:00:00 он был занят отправкой картинки.
    midnight = time(hour=0, minute=0, second=0, tzinfo=pytz.timezone(TZ_NAME))
    
    jq.run_daily(daily_report_job, time=midnight, job_kwargs={'misfire_grace_time': 60})

    print("Bot started")
    application.run_polling()