"""Chat and user settings.

Personal settings belong to one Telegram user and only that user changes them.
Group settings belong to a chat and only its Telegram administrators change
them (checked by the handlers at the moment of the change). Bot admins
(ADMIN_IDS) get no extra say over groups: running the bot and administering
a group are different roles.

Missing rows mean defaults, so a chat or user that never touched /settings
costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from sbahelper.db import Database, utc_text


@dataclass(frozen=True, slots=True)
class ChatSettings:
    chat_id: int
    # React to links in ordinary messages (needs Privacy Mode off or admin rights).
    auto_download: bool = True
    # Forum topic for scheduled summaries; None = the main chat.
    summary_thread_id: int | None = None
    # False after the bot was removed or blocked: no scheduled messages.
    active: bool = True


@dataclass(frozen=True, slots=True)
class UserSettings:
    user_id: int
    stats_opt_out: bool = False


class Preferences:
    def __init__(self, db: Database) -> None:
        self.db = db

    def chat(self, chat_id: int) -> ChatSettings:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT auto_download, summary_thread_id, active FROM chat_settings "
                "WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return ChatSettings(chat_id)
        return ChatSettings(chat_id, bool(row[0]), row[1], bool(row[2]))

    def update_chat(self, chat_id: int, **changes: object) -> ChatSettings:
        allowed = {"auto_download", "summary_thread_id", "active"}
        if unknown := set(changes) - allowed:
            raise ValueError(f"unknown chat settings: {sorted(unknown)}")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO chat_settings (chat_id, updated_at) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO NOTHING",
                (chat_id, utc_text()),
            )
            for column, value in changes.items():
                stored = int(value) if isinstance(value, bool) else value
                conn.execute(
                    f"UPDATE chat_settings SET {column} = ?, updated_at = ? WHERE chat_id = ?",
                    (stored, utc_text(), chat_id),
                )
        return self.chat(chat_id)

    def user(self, user_id: int) -> UserSettings:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT stats_opt_out FROM user_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
        return UserSettings(user_id, bool(row[0])) if row else UserSettings(user_id)

    def set_stats_opt_out(self, user_id: int, opt_out: bool) -> UserSettings:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO user_settings (user_id, stats_opt_out, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET stats_opt_out = excluded.stats_opt_out, "
                "updated_at = excluded.updated_at",
                (user_id, int(opt_out), utc_text()),
            )
        return self.user(user_id)
