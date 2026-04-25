"""
Обработчики команд и callback-кнопок Telegram-бота.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import ContextTypes

from bot.api import (
    add_media_to_playlist,
    find_media_file,
    get_playlist_info,
    get_playlist_songs,
    get_queue_data,
    get_schedule,
    get_station_data,
    is_media_in_playlist,
    skip_song_api,
)
from bot.formatters import (
    clean_track_info,
    format_changelog,
    format_main_message,
    format_playlist_announcement,
    format_queue_list,
    format_votes_message,
    get_keyboard,
)
from bot.state import (
    CHATS_DB,
    LAST_MSG_STATE,
    VOTE_STATE,
    can_user_vote,
    decrement_song_votes,
    get_recent_songs,
    get_skip_progress,
    get_song_votes,
    get_user_votes_summary,
    increment_song_votes,
    is_radio_decommissioned,
    is_song_in_best,
    mark_song_as_best,
    record_user_vote,
    remove_all_user_votes_for_song,
    remove_user_vote,
    save_chats,
    update_vote_logic,
)
from core.config import ADMIN_IDS, BEST_PLAYLIST_ID, UPVOTE_THRESHOLD


_RADIO_CLOSED_TEXT = "📻 SBA Radio закрыто. Работает только downloader bot."


async def _reply_radio_closed(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(_RADIO_CLOSED_TEXT)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start — отправляет плеер и очередь в чат (только для админов)."""
    if is_radio_decommissioned():
        await _reply_radio_closed(update)
        return

    if ADMIN_IDS and update.effective_user.id not in ADMIN_IDS:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
        except Exception:
            pass
        return

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

    data = await get_station_data()
    queue = await get_queue_data()
    schedule = await get_schedule(rows=24)

    if not data:
        await update.message.reply_text("⚠️ API недоступно.")
        return

    main_text, art, listeners, song_id = format_main_message(data)
    update_vote_logic(song_id)
    kb = get_keyboard(listeners, song_id)
    queue_text = format_queue_list(queue, schedule=schedule)

    try:
        msg_main = await update.message.reply_photo(
            photo=art, caption=main_text, reply_markup=kb, parse_mode='MarkdownV2'
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
    """Обрабатывает нажатия inline-кнопок."""
    query = update.callback_query
    user_id = query.from_user.id

    if is_radio_decommissioned():
        await query.answer(_RADIO_CLOSED_TEXT, show_alert=True)
        return

    # --- Changelog wizard: skip notes step ---
    if query.data == "cl_skip_notes":
        session = _CHANGELOG_SESSIONS.get(user_id)
        if session:
            session['data']['notes'] = ''
            await _cl_publish_changelog(context.bot, user_id, session)
            del _CHANGELOG_SESSIONS[user_id]
        await query.answer()
        return

    data = await get_station_data()
    if not data:
        await query.answer("⚠️ API недоступно.", show_alert=True)
        return

    listeners = data['listeners']['total']
    song_id = data['now_playing']['song'].get('id', data['now_playing']['song'].get('text'))

    # --- Кнопка «Пропустить» ---
    if query.data == "vote_skip":
        votes, required = get_skip_progress(listeners)

        if user_id in VOTE_STATE['voters']:
            await query.answer("Уже голосовали!", show_alert=True)
            return

        VOTE_STATE['voters'].add(user_id)
        votes += 1

        if votes >= required:
            success, msg = await skip_song_api()
            msg_text = "✅ Пропускаем!" if success else f"Ошибка: {msg}"
            await query.answer(msg_text, show_alert=True)
        else:
            await query.answer(f"Голос принят! ({votes}/{required})")

        try:
            await query.edit_message_reply_markup(reply_markup=get_keyboard(listeners, song_id))
        except Exception:
            pass
        return

    # --- Кнопка «Уже в лучших» (информационная) ---
    if query.data == "raise_already_best":
        await query.answer("✅ Эта песня уже в плейлисте лучших!", show_alert=True)
        return

    # --- Кнопка «Поднять» (тоггл: повторное нажатие отменяет голос) ---
    if query.data == "vote_raise":
        # Проверка: песня уже в лучших (локальный кеш)
        if is_song_in_best(song_id):
            await query.answer("✅ Эта песня уже в плейлисте лучших!", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=get_keyboard(listeners, song_id))
            except Exception:
                pass
            return

        # Уже голосовал сегодня — отменяем голос (toggle)
        if not can_user_vote(user_id, song_id):
            remove_user_vote(user_id, song_id)
            new_count = decrement_song_votes(song_id)
            await query.answer(
                f"↩️ Голос снят. Счёт: {new_count}/{UPVOTE_THRESHOLD}",
                show_alert=False,
            )
            try:
                await query.edit_message_reply_markup(reply_markup=get_keyboard(listeners, song_id))
            except Exception:
                pass
            return

        # Проверяем через API — вдруг песня уже была в плейлисте 16 до нас
        song_data = data['now_playing']['song']
        artist = song_data.get('artist', '')
        title = song_data.get('title', '')
        if await is_media_in_playlist(song_id, BEST_PLAYLIST_ID):
            mark_song_as_best(song_id)
            await query.answer("✅ Эта песня уже в плейлисте лучших!", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=get_keyboard(listeners, song_id))
            except Exception:
                pass
            return

        # Начисляем голос
        record_user_vote(user_id, song_id)
        track_title = clean_track_info(
            song_data.get('artist', ''),
            song_data.get('title', ''),
            song_data.get('text', ''),
        )
        new_count = increment_song_votes(song_id, title=track_title)

        if new_count >= UPVOTE_THRESHOLD:
            # Отвечаем сразу, до тяжёлых API-запросов
            await query.answer(
                f"⏳ Набрано {new_count} голосов! Добавляю в лучшие...",
                show_alert=True,
            )
            try:
                await query.edit_message_reply_markup(reply_markup=get_keyboard(listeners, song_id))
            except Exception:
                pass
            # Добавляем в плейлист 16
            media = await find_media_file(song_id, artist, title)
            if media:
                ok, result = await add_media_to_playlist(media['id'], BEST_PLAYLIST_ID)
                if ok:
                    mark_song_as_best(song_id)
                else:
                    logging.error(f"Failed to add {song_id} to playlist: {result}")
            else:
                logging.warning(f"Media file not found for song_id={song_id}")
        else:
            await query.answer(
                f"⬆️ Голос принят! Счёт: {new_count}/{UPVOTE_THRESHOLD}",
                show_alert=False,
            )
            try:
                await query.edit_message_reply_markup(reply_markup=get_keyboard(listeners, song_id))
            except Exception:
                pass
        return

    # --- Фильтры /votes view ---
    if query.data.startswith("vview_"):
        filter_mode = query.data[6:]
        if filter_mode not in ('all', 'added', 'notadded'):
            await query.answer()
            return
        await query.answer()
        await _send_votes_view(
            query, filter_mode, edit=True,
            context=context, session_msg_id=query.message.message_id,
        )
        return

    # --- Кнопка «Снять все голоса» (из меню /votes edit) ---
    if query.data.startswith("rmvote_"):
        target_song_id = query.data[7:]
        removed = remove_all_user_votes_for_song(user_id, target_song_id)
        if removed:
            await query.answer(f"🗑 Голоса сняты ({removed} шт.)")
        else:
            await query.answer("Голосов не найдено.")
        # Обновляем меню
        summary = get_user_votes_summary(user_id)
        base_text_edit = "🗳 *Ваши голоса:*\nНажмите кнопку чтобы снять _все_ свои голоса за эту песню."
        if not summary:
            base_text_edit = "🗳 *Ваши голоса:*\n\n💭 Голосов нет."
            await _reset_votes_autodelete(
                context, query.message.chat_id, query.message.message_id,
                new_base_text=base_text_edit, new_kb=None,
            )
            return
        rows = []
        for item in summary:
            cb = f"rmvote_{item['song_id']}"
            if len(cb.encode()) <= 64:
                label = f"❌ {item['title'][:35]} (+{item['user_votes']})"
                rows.append([InlineKeyboardButton(label, callback_data=cb)])
        new_kb = InlineKeyboardMarkup(rows) if rows else None
        await _reset_votes_autodelete(
            context, query.message.chat_id, query.message.message_id,
            new_base_text=base_text_edit, new_kb=new_kb,
        )
        return

    # --- Кнопка «Проголосовать за старую песню» (из меню /votes create) ---
    if query.data.startswith("vsong_"):
        target_song_id = query.data[6:]

        if is_song_in_best(target_song_id):
            await query.answer("✅ Эта песня уже в плейлисте лучших!", show_alert=True)
            return

        if not can_user_vote(user_id, target_song_id):
            await query.answer("⏳ Сегодня вы уже голосовали за эту песню.", show_alert=True)
            return

        if await is_media_in_playlist(target_song_id, BEST_PLAYLIST_ID):
            mark_song_as_best(target_song_id)
            await query.answer("✅ Эта песня уже в плейлисте лучших!", show_alert=True)
            return

        # Получаем данные трека из истории недавних
        recent = {s['song_id']: s for s in get_recent_songs()}
        song_info = recent.get(target_song_id, {})
        track_title = song_info.get('display_title', f'ID: {target_song_id[:12]}…')
        s_artist = song_info.get('artist', '')
        s_title = song_info.get('title', '')

        record_user_vote(user_id, target_song_id)
        new_count = increment_song_votes(target_song_id, title=track_title)

        if new_count >= UPVOTE_THRESHOLD:
            await query.answer(
                f"⏳ Набрано {new_count} голосов! Добавляю в лучшие...",
                show_alert=True,
            )
            media = await find_media_file(target_song_id, s_artist, s_title)
            if media:
                ok, result = await add_media_to_playlist(media['id'], BEST_PLAYLIST_ID)
                if ok:
                    mark_song_as_best(target_song_id)
                else:
                    logging.error(f"Failed to add {target_song_id} to playlist: {result}")
            else:
                logging.warning(f"Media file not found for song_id={target_song_id}")
        else:
            await query.answer(
                f"⬆️ Голос принят! Счёт: {new_count}/{UPVOTE_THRESHOLD}",
                show_alert=False,
            )

        # Обновляем кнопки в сообщении create
        recent_songs = get_recent_songs()[:3]
        if recent_songs:
            rows = []
            for s in recent_songs:
                sid = s['song_id']
                cb = f"vsong_{sid}"
                if len(cb.encode()) > 64:
                    continue
                already_best = is_song_in_best(sid)
                voted_today = not can_user_vote(user_id, sid)
                cnt = get_song_votes(sid)
                if already_best:
                    icon = "✅"
                elif voted_today:
                    icon = "✔️"
                else:
                    icon = "⬆️"
                label = f"{icon} {s['display_title'][:35]} ({cnt}/{UPVOTE_THRESHOLD})"
                rows.append([InlineKeyboardButton(label, callback_data=cb)])
            await _reset_votes_autodelete(
                context, query.message.chat_id, query.message.message_id,
                new_kb=InlineKeyboardMarkup(rows),
            )
        return


async def announcement_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /announcement playlist <id> — отправляет анонс плейлиста в чат.
    Сообщение отправителя удаляется.
    """
    if is_radio_decommissioned():
        await _reply_radio_closed(update)
        return

    # Delete the sender's command message immediately
    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
        )
    except Exception:
        pass

    if ADMIN_IDS and update.effective_user.id not in ADMIN_IDS:
        return

    args = context.args

    # --- /announcement changes ---
    if args and args[0].lower() == 'changes':
        user_id = update.effective_user.id
        group_chat_id = update.effective_chat.id
        _CHANGELOG_SESSIONS[user_id] = {
            'group_chat_id': group_chat_id,
            'step': 1,
            'data': {'version': '', 'additions': '', 'changes': '', 'deletions': '', 'notes': ''},
        }
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "📢 *Создание лога обновлений*\n"
                    f"Лог будет опубликован в чате `{group_chat_id}`.\n\n"
                    "Отвечайте на каждый вопрос отдельным сообщением."
                ),
                parse_mode='Markdown',
            )
            await _cl_send_step(context.bot, user_id, 1)
        except Exception as e:
            logging.error(f"Changelog: failed to start DM wizard for {user_id}: {e}")
            await context.bot.send_message(
                chat_id=group_chat_id,
                text="❌ Не удалось отправить ЛС. Убедитесь, что вы начали диалог с ботом.",
            )
        return

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

    playlist_info = await get_playlist_info(playlist_id)
    if not playlist_info:
        await loading_msg.edit_text(f"❌ Плейлист с ID {playlist_id} не найден или API недоступен.")
        return

    songs = await get_playlist_songs(playlist_id)
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


# ---------------------------------------------------------------------------
# /announcement changes — Changelog DM wizard
# ---------------------------------------------------------------------------

_CHANGELOG_SESSIONS: dict[int, dict] = {}
# user_id -> {group_chat_id, step (1-5), data: {version, additions, changes, deletions, notes}}

_CL_STEPS: dict[int, tuple[str, str]] = {
    1: (
        "📋 *Шаг 1/5 — Версия и название*\n"
        "_Пример: v2.1.0 — Обновление плеера_",
        "version",
    ),
    2: (
        "➕ *Шаг 2/5 — Добавлено*\n"
        "Перечислите через запятую или построчно:\n"
        "_Пример: Новая кнопка, Улучшенный плеер_",
        "additions",
    ),
    3: (
        "✏️ *Шаг 3/5 — Изменено*\n"
        "Перечислите через запятую или построчно:",
        "changes",
    ),
    4: (
        "🗑 *Шаг 4/5 — Удалено*\n"
        "Перечислите через запятую или построчно:",
        "deletions",
    ),
    5: (
        "📝 *Шаг 5/5 — Примечания* _(необязательно)_\n"
        "Отправьте текст или нажмите «Пропустить».",
        "notes",
    ),
}


def _cl_step_keyboard(step: int) -> InlineKeyboardMarkup | None:
    if step == 5:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("⏭ Пропустить", callback_data="cl_skip_notes")]]
        )
    return None


async def _cl_send_step(bot, user_id: int, step: int) -> None:
    prompt, _ = _CL_STEPS[step]
    kb = _cl_step_keyboard(step)
    try:
        await bot.send_message(
            chat_id=user_id,
            text=prompt,
            parse_mode='Markdown',
            reply_markup=kb,
        )
    except Exception as e:
        logging.warning(f"Changelog: cannot DM user {user_id} at step {step}: {e}")


async def _cl_publish_changelog(bot, user_id: int, session: dict) -> None:
    """Formats and sends the collected changelog to the source group as one message."""
    d = session['data']
    text = format_changelog(
        version=d['version'],
        additions=d['additions'],
        changes=d['changes'],
        deletions=d['deletions'],
        notes=d['notes'],
    )
    group_chat_id = session['group_chat_id']
    try:
        await bot.send_message(
            chat_id=group_chat_id,
            text=text,
            parse_mode='Markdown',
        )
    except Exception as e:
        logging.error(f"Changelog: failed to post to group {group_chat_id}: {e}")
    try:
        await bot.send_message(chat_id=user_id, text="✅ Лог обновлений опубликован!")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /votes message auto-delete – countdown in message footer
# ---------------------------------------------------------------------------
_VOTES_SESSIONS: dict[int, dict] = {}  # bot_msg_id -> {chat_id, user_msg_id, base_text, kb, parse_mode}
_VOTES_AUTODEL_SEC = 60
_VOTES_TICKS = [45, 30, 15]


def _with_timer(text: str, secs: int) -> str:
    return f"{text}\n\n_🗑 Удалится через {secs} с_"


def _cancel_votes_jobs(context: ContextTypes.DEFAULT_TYPE, chat_id, bot_msg_id: int) -> None:
    for name in (
        [f"votes_tick_{chat_id}_{bot_msg_id}_{r}" for r in _VOTES_TICKS]
        + [f"votes_del_{chat_id}_{bot_msg_id}"]
        + [f"votes_autodel_{chat_id}_{bot_msg_id}"]  # legacy
    ):
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()


def _schedule_votes_autodelete(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id,
    bot_msg_id: int,
    user_msg_id: int,
    base_text: str,
    kb: InlineKeyboardMarkup | None = None,
    parse_mode: str = 'Markdown',
) -> None:
    _cancel_votes_jobs(context, chat_id, bot_msg_id)
    _VOTES_SESSIONS[bot_msg_id] = {
        'chat_id': chat_id,
        'user_msg_id': user_msg_id,
        'base_text': base_text,
        'kb': kb,
        'parse_mode': parse_mode,
    }
    base = {'chat_id': chat_id, 'bot_msg_id': bot_msg_id, 'user_msg_id': user_msg_id}
    for remaining in _VOTES_TICKS:
        context.job_queue.run_once(
            _votes_tick_job,
            when=_VOTES_AUTODEL_SEC - remaining,
            name=f"votes_tick_{chat_id}_{bot_msg_id}_{remaining}",
            data={**base, 'remaining': remaining},
        )
    context.job_queue.run_once(
        _votes_autodelete_job,
        when=_VOTES_AUTODEL_SEC,
        name=f"votes_del_{chat_id}_{bot_msg_id}",
        data=base,
    )


async def _reset_votes_autodelete(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id,
    bot_msg_id: int,
    new_base_text: str | None = None,
    new_kb: InlineKeyboardMarkup | None = ...,  # type: ignore[assignment]
) -> None:
    """Resets the 60 s timer; optionally updates text/keyboard."""
    session = _VOTES_SESSIONS.get(bot_msg_id)
    if not session:
        return
    if new_base_text is not None:
        session['base_text'] = new_base_text
    if new_kb is not ...:  # type: ignore[comparison-overlap]
        session['kb'] = new_kb
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=bot_msg_id,
            text=_with_timer(session['base_text'], _VOTES_AUTODEL_SEC),
            reply_markup=session['kb'],
            parse_mode=session['parse_mode'],
        )
    except Exception:
        pass
    _schedule_votes_autodelete(
        context, chat_id, bot_msg_id,
        session['user_msg_id'], session['base_text'],
        session['kb'], session['parse_mode'],
    )


async def _votes_tick_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    session = _VOTES_SESSIONS.get(d['bot_msg_id'])
    if not session:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=d['chat_id'],
            message_id=d['bot_msg_id'],
            text=_with_timer(session['base_text'], d['remaining']),
            reply_markup=session['kb'],
            parse_mode=session['parse_mode'],
        )
    except Exception:
        pass


async def _votes_autodelete_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    _VOTES_SESSIONS.pop(d['bot_msg_id'], None)
    for msg_id in (d['bot_msg_id'], d['user_msg_id']):
        try:
            await context.bot.delete_message(chat_id=d['chat_id'], message_id=msg_id)
        except Exception:
            pass


async def _send_votes_view(target, filter_mode: str, edit: bool = False, context=None, session_msg_id: int = 0):
    """Sends or edits the votes view message. Returns (Message|None, base_text, kb)."""
    base_text = format_votes_message(filter_mode=filter_mode)
    kb = _votes_view_keyboard(filter_mode)
    if edit:
        if context and session_msg_id:
            await _reset_votes_autodelete(context, target.message.chat_id, session_msg_id,
                                          new_base_text=base_text, new_kb=kb)
        else:
            try:
                await target.edit_message_text(
                    _with_timer(base_text, _VOTES_AUTODEL_SEC),
                    parse_mode='Markdown', reply_markup=kb,
                )
            except Exception:
                pass
        return None, base_text, kb
    else:
        sent = await target.reply_text(
            _with_timer(base_text, _VOTES_AUTODEL_SEC),
            parse_mode='Markdown', reply_markup=kb,
        )
        return sent, base_text, kb


def _votes_view_keyboard(active: str) -> InlineKeyboardMarkup:
    """Returns the filter keyboard for /votes view, highlighting the active filter."""
    def label(text: str, key: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text + (" ◀" if active == key else ""),
            callback_data=f"vview_{key}",
        )
    return InlineKeyboardMarkup([[
        label("📋 Все", "all"),
        label("✅ Добавленные", "added"),
        label("⏳ Не добавлены", "notadded"),
    ]])


async def votes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /votes [view|edit|create] — управление голосами за треки.
    /votes view    — список голосов с кнопками фильтрации (все / добавленные / не добавленные)
    /votes edit    — личное меню с кнопками удаления голосов
    /votes create  — проголосовать за одну из последних песен
    """
    if is_radio_decommissioned():
        await _reply_radio_closed(update)
        return

    args = context.args

    if not args:
        _help_text = (
            "ℹ️ *Голоса за треки:*\n"
            "`/votes view` — список голосов\n"
            "`/votes edit` — снять свои голоса\n"
            "`/votes create` — проголосовать за недавнюю песню"
        )
        sent = await update.message.reply_text(
            _with_timer(_help_text, _VOTES_AUTODEL_SEC),
            parse_mode='Markdown',
        )
        _schedule_votes_autodelete(
            context,
            update.effective_chat.id,
            sent.message_id,
            update.message.message_id,
            base_text=_help_text,
        )
        return

    subcommand = args[0].lower()

    # --- /votes view ---
    if subcommand == 'view':
        filter_mode = 'all'
        rest = args[1:]
        if rest and rest[0].lower() in ('added', 'notadded'):
            filter_mode = rest[0].lower()
        sent, base_text, kb = await _send_votes_view(update.message, filter_mode, edit=False)
        if sent:
            _schedule_votes_autodelete(
                context,
                update.effective_chat.id,
                sent.message_id,
                update.message.message_id,
                base_text=base_text,
                kb=kb,
            )
        return

    # --- /votes edit ---
    if subcommand == 'edit':
        user_id = update.effective_user.id
        summary = get_user_votes_summary(user_id)
        if not summary:
            base_text = "🗳 *Ваши голоса:*\n\n💭 Вы ещё ни за что не голосовали."
            sent = await update.message.reply_text(
                _with_timer(base_text, _VOTES_AUTODEL_SEC), parse_mode='Markdown',
            )
            _schedule_votes_autodelete(
                context, update.effective_chat.id,
                sent.message_id, update.message.message_id, base_text=base_text,
            )
            return
        rows = []
        for item in summary:
            cb = f"rmvote_{item['song_id']}"
            if len(cb.encode()) > 64:
                continue
            status = " ✅" if item['in_best'] else ""
            label = f"❌ {item['title'][:35]}{status} (+{item['user_votes']})"
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
        base_text = "🗳 *Ваши голоса:*\nНажмите кнопку чтобы снять _все_ свои голоса за эту песню."
        kb = InlineKeyboardMarkup(rows) if rows else None
        sent = await update.message.reply_text(
            _with_timer(base_text, _VOTES_AUTODEL_SEC),
            reply_markup=kb,
            parse_mode='Markdown',
        )
        _schedule_votes_autodelete(
            context, update.effective_chat.id,
            sent.message_id, update.message.message_id,
            base_text=base_text, kb=kb,
        )
        return

    # --- /votes create ---
    if subcommand == 'create':
        user_id = update.effective_user.id
        recent_songs = get_recent_songs()[:3]
        if not recent_songs:
            base_text = "⚠️ Список недавних песен пуст. Подождите смены трека."
            sent = await update.message.reply_text(_with_timer(base_text, _VOTES_AUTODEL_SEC))
            _schedule_votes_autodelete(
                context, update.effective_chat.id,
                sent.message_id, update.message.message_id, base_text=base_text,
            )
            return
        rows = []
        for s in recent_songs:
            sid = s['song_id']
            cb = f"vsong_{sid}"
            if len(cb.encode()) > 64:
                continue
            already_best = is_song_in_best(sid)
            voted_today = not can_user_vote(user_id, sid)
            cnt = get_song_votes(sid)
            if already_best:
                icon = "✅"
            elif voted_today:
                icon = "✔️"
            else:
                icon = "⬆️"
            label = f"{icon} {s['display_title'][:35]} ({cnt}/{UPVOTE_THRESHOLD})"
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
        if not rows:
            base_text = "⚠️ Нет доступных песен для голосования."
            sent = await update.message.reply_text(_with_timer(base_text, _VOTES_AUTODEL_SEC))
            _schedule_votes_autodelete(
                context, update.effective_chat.id,
                sent.message_id, update.message.message_id, base_text=base_text,
            )
            return
        base_text = "🎵 *Недавние песни:*\nВыберите песню для голосования."
        kb = InlineKeyboardMarkup(rows)
        sent = await update.message.reply_text(
            _with_timer(base_text, _VOTES_AUTODEL_SEC),
            reply_markup=kb,
            parse_mode='Markdown',
        )
        _schedule_votes_autodelete(
            context, update.effective_chat.id,
            sent.message_id, update.message.message_id,
            base_text=base_text, kb=kb,
        )
        return

    await update.message.reply_text(
        "❌ Неизвестная подкоманда. Используйте `/votes view`, `/votes edit` или `/votes create`.",
        parse_mode='Markdown',
    )


async def changelog_dm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles private DM text messages from admins filling in the changelog wizard.
    Advances the session step by step; publishes to the group when all steps are done.
    """
    user_id = update.effective_user.id
    session = _CHANGELOG_SESSIONS.get(user_id)
    if not session:
        return  # Not in a changelog session — ignore

    step = session['step']
    _, field = _CL_STEPS[step]
    session['data'][field] = update.message.text.strip()

    if step < 5:
        session['step'] += 1
        await _cl_send_step(context.bot, user_id, session['step'])
    else:
        # Step 5 complete — publish
        await _cl_publish_changelog(context.bot, user_id, session)
        del _CHANGELOG_SESSIONS[user_id]

