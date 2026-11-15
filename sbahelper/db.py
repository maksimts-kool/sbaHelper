"""The SQLite database: connections, versioned migrations and online backups.

One file (STATS_DB_PATH) holds statistics, the Telegram file cache, chat and
user settings and a little operational state. Every call opens a short-lived
connection, so the store is safe to use from worker threads; WAL mode lets
readers run while one writer commits, and `busy_timeout` makes a writer wait
for another instead of failing at once.

Migrations
----------
`PRAGMA user_version` is the schema version. Each migration runs in its own
`BEGIN IMMEDIATE` transaction together with the version bump, so a failed
migration rolls back completely and is retried on the next start; there is
no half-applied state. Before migrating a database that already has data, a
consistent copy is written next to it (`<file>.pre-v<N>-<time>.bak`) with
SQLite's online backup API, which is the rollback path. A database newer than
this code (after a downgrade) is refused rather than written to.

Version 1 is the schema the bot had before migrations existed; it is created
with `IF NOT EXISTS` plus the old `username` column guard, so production
databases of any earlier shape are adopted without data loss.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5000

type Step = str | Callable[[sqlite3.Connection], None]


class SchemaTooNew(RuntimeError):
    """The database was written by a newer version of the bot."""


def _v1_baseline(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT    NOT NULL,
            chat_id      INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            user_name    TEXT    NOT NULL,
            username     TEXT,
            platform     TEXT    NOT NULL,
            duration_sec INTEGER NOT NULL,
            size_bytes   INTEGER NOT NULL,
            title        TEXT    NOT NULL,
            uploader     TEXT    NOT NULL,
            view_count   INTEGER
        )
        """
    )
    # Databases from before usernames were stored lack the column.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(downloads)")}
    if "username" not in columns:
        conn.execute("ALTER TABLE downloads ADD COLUMN username TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS downloads_chat_created_idx ON downloads (chat_id, created_at)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS downloads_created_idx ON downloads (created_at)")


MIGRATIONS: list[tuple[int, str, list[Step]]] = [
    (1, "downloads log (pre-migration schema)", [_v1_baseline]),
    (
        2,
        "Telegram file cache and URL aliases",
        [
            # One row per (bot, video). No user data: who asked is not stored here.
            """
            CREATE TABLE video_cache (
                bot_id       INTEGER NOT NULL,
                video_key    TEXT    NOT NULL,
                policy       INTEGER NOT NULL,
                file_id      TEXT    NOT NULL,
                media_kind   TEXT    NOT NULL,
                platform     TEXT    NOT NULL,
                title        TEXT    NOT NULL,
                uploader     TEXT    NOT NULL,
                duration_sec INTEGER NOT NULL,
                width        INTEGER,
                height       INTEGER,
                view_count   INTEGER,
                like_count   INTEGER,
                size_bytes   INTEGER NOT NULL,
                created_at   TEXT    NOT NULL,
                last_used_at TEXT    NOT NULL,
                PRIMARY KEY (bot_id, video_key)
            )
            """,
            "CREATE INDEX video_cache_created_idx ON video_cache (created_at)",
            # Short links → the video they resolved to. Bot-independent.
            """
            CREATE TABLE url_aliases (
                url_key    TEXT PRIMARY KEY,
                video_key  TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX url_aliases_created_idx ON url_aliases (created_at)",
        ],
    ),
    (
        3,
        "chat and user settings, operational state",
        [
            """
            CREATE TABLE chat_settings (
                chat_id           INTEGER PRIMARY KEY,
                auto_download     INTEGER NOT NULL DEFAULT 1,
                summary_thread_id INTEGER,
                active            INTEGER NOT NULL DEFAULT 1,
                updated_at        TEXT    NOT NULL
            )
            """,
            """
            CREATE TABLE user_settings (
                user_id       INTEGER PRIMARY KEY,
                stats_opt_out INTEGER NOT NULL DEFAULT 0,
                updated_at    TEXT    NOT NULL
            )
            """,
            """
            CREATE TABLE state (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX downloads_user_idx ON downloads (user_id)",
        ],
    ),
]
LATEST_VERSION = MIGRATIONS[-1][0]


def utc_text(moment: datetime | None = None) -> str:
    """UTC ISO-8601 without microseconds: sorts correctly as text in SQLite."""
    moment = moment or datetime.now(UTC)
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(microsecond=0).isoformat()


def _open(path: Path) -> sqlite3.Connection:
    # Autocommit mode: transactions are explicit (see `Database.transaction`).
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backup(source: Path, target: Path) -> None:
    """Consistent copy of a live database (WAL included) via the online backup API.

    Copying only the main file while WAL writes are active can lose or corrupt
    data; this does not. The copy is checked with `PRAGMA integrity_check` and
    made 0600, since it holds chat and user ids.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial")
    partial.unlink(missing_ok=True)
    with closing(_open(source)) as src, closing(sqlite3.connect(partial)) as dst:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"backup failed integrity check: {result}")
        # A backup of a WAL database is itself in WAL mode; a single file is easier to move.
        dst.execute("PRAGMA journal_mode = DELETE")
    os.chmod(partial, 0o600)
    os.replace(partial, target)


def integrity_check(path: Path) -> str:
    with closing(_open(path)) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def schema_version(path: Path) -> int:
    with closing(_open(path)) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


class Database:
    def __init__(self, path: Path, *, backup_before_migrate: bool = True) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_before_migrate = backup_before_migrate
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection for reads (autocommit: each statement is its own transaction)."""
        with closing(_open(self.path)) as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. Never hold one across network or download work."""
        with closing(_open(self.path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def ping(self) -> bool:
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    # -- migrations -------------------------------------------------------- #

    def migrate(self) -> None:
        with self.connect() as conn:
            try:
                mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            except sqlite3.Error as error:
                mode = f"unchanged ({error})"
            if str(mode).lower() != "wal":
                log.warning("WAL not enabled. Path=%s Mode=%s", self.path, mode)
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            has_data = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()
            )
        if current > LATEST_VERSION:
            raise SchemaTooNew(
                f"database schema v{current} is newer than this bot (v{LATEST_VERSION}); "
                "restore a backup or run the newer version"
            )
        if current == LATEST_VERSION:
            return
        if has_data and self._backup_before_migrate:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            copy = self.path.with_name(f"{self.path.name}.pre-v{LATEST_VERSION}-{stamp}.bak")
            backup(self.path, copy)
            log.info("Database backed up before migration. Copy=%s", copy.name)

        for version, description, steps in MIGRATIONS:
            if version <= current:
                continue
            with self.transaction() as conn:
                for step in steps:
                    if callable(step):
                        step(conn)
                    else:
                        conn.execute(step)
                conn.execute(f"PRAGMA user_version = {version}")
            log.info("Database migrated. Version=%d Step=%s", version, description)

    # -- operational state --------------------------------------------------- #

    def get_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, utc_text()),
            )

    def prune_in_batches(
        self, sql_select_rowids: str, table: str, params: tuple, batch: int = 2000
    ) -> int:
        """Delete matching rows in short transactions so writers never wait long."""
        removed = 0
        while True:
            with self.transaction() as conn:
                count = conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN ({sql_select_rowids} LIMIT {int(batch)})",
                    params,
                ).rowcount
            removed += count
            if count < batch:
                return removed
