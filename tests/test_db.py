"""Migrations, backups and the operational state table, on real SQLite files."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from sbahelper import backup as backup_cli
from sbahelper import db as dbmod
from sbahelper.db import LATEST_VERSION, Database, SchemaTooNew, schema_version
from sbahelper.stats import StatsStore

# The table as the bot created it before usernames existed (and before migrations).
OLDEST_SCHEMA = """
CREATE TABLE downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, user_name TEXT NOT NULL, platform TEXT NOT NULL,
    duration_sec INTEGER NOT NULL, size_bytes INTEGER NOT NULL, title TEXT NOT NULL,
    uploader TEXT NOT NULL, view_count INTEGER
);
INSERT INTO downloads VALUES
    (1, '2026-07-21T10:00:00+00:00', -100, 1, 'Максим', 'tiktok', 30, 1000, 'Кот', 'cat', 5),
    (2, '2026-07-21T11:00:00+00:00', -100, 2, 'Аня', 'facebook', 60, 2000, 'Пёс', 'dog', NULL);
"""


def legacy_db(path: Path, script: str = OLDEST_SCHEMA) -> Path:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(script)
    return path


def tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_gets_the_latest_schema(tmp_path: Path) -> None:
    db = Database(tmp_path / "new.db")
    assert schema_version(db.path) == LATEST_VERSION
    assert {
        "downloads",
        "video_cache",
        "url_aliases",
        "chat_settings",
        "user_settings",
        "state",
    } <= (tables(db.path))
    assert not list(tmp_path.glob("*.bak"))  # nothing to back up
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_production_database_is_migrated_without_losing_rows(tmp_path: Path) -> None:
    path = legacy_db(tmp_path / "downloader_stats.db")

    store = StatsStore(path)

    assert schema_version(path) == LATEST_VERSION
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT id, user_name, platform, username FROM downloads").fetchall()
    assert rows == [(1, "Максим", "tiktok", None), (2, "Аня", "facebook", None)]
    backups = list(tmp_path.glob("downloader_stats.db.pre-v*.bak"))
    assert len(backups) == 1 and schema_version(backups[0]) == 0
    assert backups[0].stat().st_mode & 0o777 == 0o600
    store.db.migrate()  # idempotent: no second backup
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_v1_database_with_username_upgrades(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    with closing(sqlite3.connect(path)) as conn:
        dbmod._v1_baseline(conn)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    Database(path)
    assert schema_version(path) == LATEST_VERSION


def test_failed_migration_rolls_back_and_is_retried(tmp_path: Path, monkeypatch) -> None:
    path = legacy_db(tmp_path / "s.db")
    broken = [*dbmod.MIGRATIONS[:2], (3, "broken", ["CREATE TABLE chat_settings (x)", "BOGUS SQL"])]
    monkeypatch.setattr(dbmod, "MIGRATIONS", broken)

    with pytest.raises(sqlite3.OperationalError):
        Database(path)

    assert schema_version(path) == 2  # v1 and v2 committed, v3 not at all
    assert "chat_settings" not in tables(path)
    monkeypatch.undo()
    Database(path)
    assert schema_version(path) == LATEST_VERSION


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    with pytest.raises(SchemaTooNew):
        Database(path)


def test_online_backup_is_consistent_while_writes_happen(tmp_path: Path) -> None:
    db = Database(tmp_path / "live.db")
    db.set_state("k", "v")
    with db.transaction() as conn:  # an open WAL with uncheckpointed data
        conn.execute("INSERT INTO state VALUES ('k2', 'v2', 'now')")
    target = tmp_path / "out" / "copy.db"

    dbmod.backup(db.path, target)

    assert dbmod.integrity_check(target) == "ok"
    assert Database(target, backup_before_migrate=False).get_state("k2") == "v2"
    assert not list(target.parent.glob(".*partial"))


def test_backup_cli_create_and_verify(tmp_path: Path, capsys) -> None:
    db = Database(tmp_path / "live.db")
    db.set_state("hello", "world")
    target = tmp_path / "b.db"

    assert backup_cli.main(["create", str(target), "--source", str(db.path)]) == 0
    assert backup_cli.main(["verify", str(target)]) == 0
    assert "OK integrity=ok" in capsys.readouterr().out

    target.write_bytes(b"not a database")
    assert backup_cli.main(["verify", str(target)]) == 1


def test_state_roundtrip_and_batched_prune(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    db.set_state("a", "1")
    db.set_state("a", "2")
    assert db.get_state("a") == "2" and db.get_state("missing") is None
    for index in range(25):
        db.set_state(f"x{index}", "v")
    removed = db.prune_in_batches("SELECT rowid FROM state WHERE key LIKE ?", "state", ("x%",), 10)
    assert removed == 25 and db.ping()
