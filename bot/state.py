"""
Глобальное изменяемое состояние бота:
  - VOTE_STATE   — текущее голосование за скип трека
  - LAST_MSG_STATE — кеш последних отправленных текстов (для сравнения)
  - CHATS_DB     — активные чаты (chat_id -> {main: msg_id, queue: msg_id})
  - UPVOTES_DB   — голоса «поднять» (счётчики песен + дневные лимиты пользователей)
"""
import json
import logging
import os
from datetime import datetime

import pytz

from core.config import CHATS_FILE, TZ_NAME, UPVOTES_FILE

# --- ГОЛОСОВАНИЕ ---
VOTE_STATE: dict = {
    'song_id': None,
    'voters': set(),
}

# --- КЕШ СООБЩЕНИЙ ---
LAST_MSG_STATE: dict = {}


# --- ЧАТЫ ---

def load_chats() -> dict:
    if os.path.exists(CHATS_FILE):
        try:
            with open(CHATS_FILE, 'r') as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(v, dict) and 'main' in v}
        except Exception as e:
            logging.error(f"Error loading chats file: {e}")
    return {}


def save_chats(data: dict) -> None:
    try:
        with open(CHATS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logging.error(f"Error saving chats file: {e}")


CHATS_DB: dict = load_chats()


# --- АПВОУТЫ (поднять трек) ---

def load_upvotes() -> dict:
    if os.path.exists(UPVOTES_FILE):
        try:
            with open(UPVOTES_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading upvotes file: {e}")
    return {"songs": {}, "user_votes": {}}


def save_upvotes(data: dict) -> None:
    try:
        with open(UPVOTES_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving upvotes file: {e}")


UPVOTES_DB: dict = load_upvotes()


def _today() -> str:
    """Возвращает текущую дату в формате YYYY-MM-DD по timezone станции."""
    tz = pytz.timezone(TZ_NAME)
    return datetime.now(tz).strftime('%Y-%m-%d')


def can_user_vote(user_id: int, song_id: str) -> bool:
    """True если пользователь ещё не голосовал за эту песню сегодня."""
    today = _today()
    uid = str(user_id)
    day_votes = UPVOTES_DB['user_votes'].get(uid, {}).get(today, [])
    return song_id not in day_votes


def record_user_vote(user_id: int, song_id: str) -> None:
    """Записывает голос пользователя за песню на сегодня."""
    today = _today()
    uid = str(user_id)
    UPVOTES_DB['user_votes'].setdefault(uid, {})
    UPVOTES_DB['user_votes'][uid].setdefault(today, [])
    if song_id not in UPVOTES_DB['user_votes'][uid][today]:
        UPVOTES_DB['user_votes'][uid][today].append(song_id)
    save_upvotes(UPVOTES_DB)


def get_song_votes(song_id: str) -> int:
    """Возвращает текущий счётчик голосов песни."""
    return UPVOTES_DB['songs'].get(song_id, {}).get('count', 0)


def increment_song_votes(song_id: str) -> int:
    """Увеличивает счётчик на 1 и возвращает новое значение."""
    entry = UPVOTES_DB['songs'].setdefault(song_id, {'count': 0, 'in_best': False})
    entry['count'] += 1
    save_upvotes(UPVOTES_DB)
    return entry['count']


def remove_user_vote(user_id: int, song_id: str) -> None:
    """Удаляет голос пользователя за эту песню на сегодня."""
    today = _today()
    uid = str(user_id)
    day_votes = UPVOTES_DB['user_votes'].get(uid, {}).get(today, [])
    if song_id in day_votes:
        day_votes.remove(song_id)
        UPVOTES_DB['user_votes'][uid][today] = day_votes
        save_upvotes(UPVOTES_DB)


def decrement_song_votes(song_id: str) -> int:
    """Уменьшает счётчик на 1 (минимум 0) и возвращает новое значение."""
    entry = UPVOTES_DB['songs'].get(song_id)
    if entry and entry['count'] > 0:
        entry['count'] -= 1
        save_upvotes(UPVOTES_DB)
        return entry['count']
    return 0


def is_song_in_best(song_id: str) -> bool:
    """True если песня уже добавлена в плейлист лучших."""
    return UPVOTES_DB['songs'].get(song_id, {}).get('in_best', False)


def mark_song_as_best(song_id: str) -> None:
    """Помечает песню как добавленную в плейлист лучших."""
    UPVOTES_DB['songs'].setdefault(song_id, {'count': 0, 'in_best': False})
    UPVOTES_DB['songs'][song_id]['in_best'] = True
    save_upvotes(UPVOTES_DB)


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def update_vote_logic(current_song_id) -> bool:
    """Сбрасывает голосование если сменился трек. Возвращает True при сбросе."""
    global VOTE_STATE
    if VOTE_STATE['song_id'] != current_song_id:
        VOTE_STATE['song_id'] = current_song_id
        VOTE_STATE['voters'] = set()
        return True
    return False


def get_skip_progress(total_listeners: int) -> tuple[int, int]:
    """Возвращает (текущие_голоса, нужно_голосов)."""
    required = 1 if total_listeners <= 1 else int(total_listeners * 0.5)
    return len(VOTE_STATE['voters']), required
