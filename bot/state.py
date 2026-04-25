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

from core.config import (
    CHATS_FILE,
    FAREWELL_NOTICE_STATE_FILE,
    RADIO_DECOMMISSION_STATE_FILE,
    SCHEDULE_NOTIFY_STATE_FILE,
    TZ_NAME,
    UPVOTES_FILE,
)

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


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _normalize_schedule_keys(raw_keys) -> set[tuple[int, int]]:
    keys: set[tuple[int, int]] = set()
    if not isinstance(raw_keys, list):
        return keys

    for item in raw_keys:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            keys.add((int(item[0]), int(item[1])))
        except (TypeError, ValueError):
            continue

    return keys


def _normalize_schedule_message_map(raw_messages) -> dict[str, int]:
    messages: dict[str, int] = {}
    if not isinstance(raw_messages, dict):
        return messages

    for chat_id, message_id in raw_messages.items():
        try:
            messages[str(chat_id)] = int(message_id)
        except (TypeError, ValueError):
            continue

    return messages


def load_schedule_notify_state() -> dict[str, object]:
    if os.path.exists(SCHEDULE_NOTIFY_STATE_FILE):
        try:
            with open(SCHEDULE_NOTIFY_STATE_FILE, 'r') as f:
                data = json.load(f)
            return {
                'date': str(data.get('date') or ''),
                'signature': str(data.get('signature') or ''),
                'text': str(data.get('text') or ''),
                'messages': _normalize_schedule_message_map(data.get('messages', {})),
            }
        except Exception as e:
            logging.error(f"Error loading schedule notify state: {e}")
    return {
        'date': '',
        'signature': '',
        'text': '',
        'messages': {},
    }


def save_schedule_notify_state(state: dict[str, object]) -> None:
    try:
        _ensure_parent_dir(SCHEDULE_NOTIFY_STATE_FILE)
        payload = {
            'date': str(state.get('date') or ''),
            'signature': str(state.get('signature') or ''),
            'text': str(state.get('text') or ''),
            'messages': {
                str(chat_id): int(message_id)
                for chat_id, message_id in dict(state.get('messages') or {}).items()
            },
        }
        with open(SCHEDULE_NOTIFY_STATE_FILE, 'w') as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving schedule notify state: {e}")


def load_farewell_notice_state() -> dict[str, object]:
    if os.path.exists(FAREWELL_NOTICE_STATE_FILE):
        try:
            with open(FAREWELL_NOTICE_STATE_FILE, 'r') as f:
                data = json.load(f)
            return {
                'date': str(data.get('date') or ''),
                'messages': _normalize_schedule_message_map(data.get('messages', {})),
            }
        except Exception as e:
            logging.error(f"Error loading farewell notice state: {e}")
    return {
        'date': '',
        'messages': {},
    }


def save_farewell_notice_state(state: dict[str, object]) -> None:
    try:
        _ensure_parent_dir(FAREWELL_NOTICE_STATE_FILE)
        payload = {
            'date': str(state.get('date') or ''),
            'messages': {
                str(chat_id): int(message_id)
                for chat_id, message_id in dict(state.get('messages') or {}).items()
            },
        }
        with open(FAREWELL_NOTICE_STATE_FILE, 'w') as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving farewell notice state: {e}")


def load_radio_decommission_state() -> dict[str, object]:
    if os.path.exists(RADIO_DECOMMISSION_STATE_FILE):
        try:
            with open(RADIO_DECOMMISSION_STATE_FILE, 'r') as f:
                data = json.load(f)
            return {
                'completed': bool(data.get('completed')),
                'completed_at': str(data.get('completed_at') or ''),
            }
        except Exception as e:
            logging.error(f"Error loading radio decommission state: {e}")
    return {
        'completed': False,
        'completed_at': '',
    }


def save_radio_decommission_state(state: dict[str, object]) -> None:
    try:
        _ensure_parent_dir(RADIO_DECOMMISSION_STATE_FILE)
        payload = {
            'completed': bool(state.get('completed')),
            'completed_at': str(state.get('completed_at') or ''),
        }
        with open(RADIO_DECOMMISSION_STATE_FILE, 'w') as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving radio decommission state: {e}")


def is_radio_decommissioned() -> bool:
    return bool(load_radio_decommission_state().get('completed'))


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


def increment_song_votes(song_id: str, title: str = None) -> int:
    """Увеличивает счётчик на 1 и возвращает новое значение. Сохраняет название трека если передано."""
    entry = UPVOTES_DB['songs'].setdefault(song_id, {'count': 0, 'in_best': False})
    entry['count'] += 1
    if title and not entry.get('title'):
        entry['title'] = title
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


def get_all_votes_data() -> list:
    """Возвращает список всех песен с их голосами, отсортированный по убыванию. Пропускает песни с 0 голосов."""
    result = []
    for song_id, data in UPVOTES_DB['songs'].items():
        count = data.get('count', 0)
        if count <= 0:
            continue
        result.append({
            'song_id': song_id,
            'count': count,
            'in_best': data.get('in_best', False),
            'title': data.get('title', f'ID: {song_id[:12]}…'),
        })
    result.sort(key=lambda x: x['count'], reverse=True)
    return result


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


# --- ИСТОРИЯ ПОСЛЕДНИХ ПЕСЕН ---

RECENT_SONGS: list = []  # [{'song_id': str, 'title': str, 'artist': str, 'display_title': str}]
_MAX_RECENT = 5


def add_recent_song(song_id: str, display_title: str, artist: str = '', title: str = '') -> None:
    """Добавляет песню в список последних (для /votes create)."""
    global RECENT_SONGS
    RECENT_SONGS = [s for s in RECENT_SONGS if s['song_id'] != song_id]
    RECENT_SONGS.insert(0, {
        'song_id': song_id,
        'display_title': display_title,
        'artist': artist,
        'title': title,
    })
    RECENT_SONGS = RECENT_SONGS[:_MAX_RECENT]


def get_recent_songs() -> list:
    """Возвращает список последних до _MAX_RECENT песен."""
    return list(RECENT_SONGS)


# --- ЛИЧНЫЕ ГОЛОСА ПОЛЬЗОВАТЕЛЯ ---

def get_user_votes_summary(user_id: int) -> list[dict]:
    """Возвращает список песен, за которые пользователь голосовал, с суммой по всем дням."""
    uid = str(user_id)
    user_day_votes = UPVOTES_DB['user_votes'].get(uid, {})
    song_vote_counts: dict[str, int] = {}
    for day_songs in user_day_votes.values():
        for song_id in day_songs:
            song_vote_counts[song_id] = song_vote_counts.get(song_id, 0) + 1

    result = []
    for song_id, user_count in song_vote_counts.items():
        song_data = UPVOTES_DB['songs'].get(song_id, {})
        result.append({
            'song_id': song_id,
            'title': song_data.get('title', f'ID: {song_id[:12]}…'),
            'user_votes': user_count,
            'total_votes': song_data.get('count', 0),
            'in_best': song_data.get('in_best', False),
        })
    result.sort(key=lambda x: x['user_votes'], reverse=True)
    return result


def remove_all_user_votes_for_song(user_id: int, song_id: str) -> int:
    """Удаляет все голоса пользователя за конкретную песню по всем дням. Возвращает кол-во удалённых."""
    uid = str(user_id)
    user_day_votes = UPVOTES_DB['user_votes'].get(uid, {})
    count_removed = 0
    for date in list(user_day_votes.keys()):
        if song_id in user_day_votes[date]:
            user_day_votes[date].remove(song_id)
            count_removed += 1
    if count_removed > 0:
        entry = UPVOTES_DB['songs'].get(song_id)
        if entry:
            entry['count'] = max(0, entry['count'] - count_removed)
        save_upvotes(UPVOTES_DB)
    return count_removed
