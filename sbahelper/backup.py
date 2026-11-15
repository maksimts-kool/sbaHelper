"""Consistent database backups and restore checks.

    python -m sbahelper.backup create [TARGET]   # online backup, safe while the bot runs
    python -m sbahelper.backup verify FILE       # integrity check + schema version

`create` uses SQLite's online backup API, so it is consistent even while the
bot writes in WAL mode (copying just the .db file is not). The copy is a
single file (journal mode DELETE), checked with `PRAGMA integrity_check` and
made 0600. Default target: `<STATS_DB_PATH dir>/backups/<name>-<UTC time>.db`.

Restore is a file copy while the bot is stopped; see README "Backup and
restore". Cookie files are credentials and are not part of this backup.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from sbahelper.db import LATEST_VERSION, backup, integrity_check, schema_version


def default_target(source: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return source.parent / "backups" / f"{source.stem}-{stamp}.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sbahelper.backup")
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("create", help="write a consistent copy of the live database")
    create.add_argument("target", nargs="?", type=Path)
    create.add_argument("--source", type=Path, help="database file (default: STATS_DB_PATH)")
    verify = sub.add_parser("verify", help="check a database or backup file")
    verify.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.action == "create":
            if args.source is None:
                from sbahelper.config import settings

                args.source = settings.stats_db_path
            if not args.source.is_file():
                print(f"FAIL no database at {args.source}")
                return 1
            target = args.target or default_target(args.source)
            backup(args.source, target)
            print(f"OK {target} (schema v{schema_version(target)})")
            return 0
        if not args.file.is_file():
            print(f"FAIL no file at {args.file}")
            return 1
        result = integrity_check(args.file)
        version = schema_version(args.file)
        status = "OK" if result == "ok" and version <= LATEST_VERSION else "FAIL"
        note = "" if version <= LATEST_VERSION else f" (newer than this bot's v{LATEST_VERSION})"
        print(f"{status} integrity={result} schema=v{version}{note}")
        return 0 if status == "OK" else 1
    except (sqlite3.Error, OSError) as error:
        print(f"FAIL {type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
