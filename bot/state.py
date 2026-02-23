"""
Глобальное изменяемое состояние бота:
  - VOTE_STATE   — текущее голосование за скип трека
  - LAST_MSG_STATE — кеш последних отправленных текстов (для сравнения)
  - CHATS_DB     — активные чаты (chat_id -> {main: msg_id, queue: msg_id})
"""
import json
import logging
import os

from core.config import CHATS_FILE

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
