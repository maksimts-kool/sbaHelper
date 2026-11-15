"""Telegram file cache, URL aliases and chat/user settings on a real database."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sbahelper.cache import FileCache, policy_fingerprint
from sbahelper.db import Database, utc_text
from sbahelper.download import VideoInfo
from sbahelper.prefs import Preferences

INFO = VideoInfo("youtube", "Кот", "cat", 30, 100, 5, 720, 1280, video_id="abcdefghijk")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "c.db")


def cache(db: Database, bot_id: int = 1, policy: int = 7, ttl: int = 30) -> FileCache:
    return FileCache(db, bot_id=bot_id, policy=policy, ttl_days=ttl)


def test_put_and_get(db: Database) -> None:
    store = cache(db)
    store.put("youtube:abcdefghijk", "FILE", "video", INFO, 1234)
    hit = store.get("youtube:abcdefghijk")
    assert hit is not None and hit.file_id == "FILE" and hit.media_kind == "video"
    assert (
        hit.info.title == "Кот" and hit.info.key == "youtube:abcdefghijk" and hit.size_bytes == 1234
    )


def test_entries_belong_to_one_bot(db: Database) -> None:
    cache(db, bot_id=1).put("youtube:abcdefghijk", "FILE", "video", INFO, 1)
    assert cache(db, bot_id=2).get("youtube:abcdefghijk") is None


def test_other_policy_is_a_miss(db: Database) -> None:
    cache(db, policy=1).put("youtube:abcdefghijk", "FILE", "video", INFO, 1)
    assert cache(db, policy=2).get("youtube:abcdefghijk") is None
    assert policy_fingerprint(2, 45_000_000, 300) != policy_fingerprint(2, 40_000_000, 300)


def test_expired_entries_are_misses_and_pruned(db: Database) -> None:
    store = cache(db, ttl=1)
    store.put("youtube:abcdefghijk", "FILE", "video", INFO, 1)
    store.remember_alias("url:vm.tiktok.com/abc", "tiktok:1")
    old = utc_text(datetime.now(UTC) - timedelta(days=2))
    with db.transaction() as conn:
        conn.execute("UPDATE video_cache SET created_at = ?", (old,))
        conn.execute("UPDATE url_aliases SET created_at = ?", (old,))
    assert store.get("youtube:abcdefghijk") is None
    assert store.resolve("url:vm.tiktok.com/abc") is None
    assert store.prune() == 2 and store.count() == 0


def test_invalidate(db: Database) -> None:
    store = cache(db)
    store.put("youtube:abcdefghijk", "FILE", "video", INFO, 1)
    store.invalidate("youtube:abcdefghijk")
    assert store.get("youtube:abcdefghijk") is None


def test_disabled_cache_stores_nothing(db: Database) -> None:
    store = cache(db, ttl=0)
    store.put("youtube:abcdefghijk", "FILE", "video", INFO, 1)
    assert not store.enabled and store.get("youtube:abcdefghijk") is None and store.count() == 0


def test_aliases_only_for_unresolved_urls(db: Database) -> None:
    store = cache(db)
    store.remember_alias("url:vm.tiktok.com/abc", "tiktok:123")
    store.remember_alias("tiktok:999", "tiktok:123")  # already an identity: ignored
    assert store.resolve("url:vm.tiktok.com/abc") == "tiktok:123"
    assert store.resolve("tiktok:999") is None


def test_cache_rows_hold_no_people(db: Database) -> None:
    cache(db).put("youtube:abcdefghijk", "FILE", "video", INFO, 1)
    with db.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(video_cache)")}
    assert not {"user_id", "chat_id", "user_name", "username"} & columns


def test_chat_and_user_settings_default_and_update(db: Database) -> None:
    prefs = Preferences(db)
    assert prefs.chat(-1).auto_download and prefs.chat(-1).active
    assert prefs.update_chat(-1, auto_download=False, summary_thread_id=42).summary_thread_id == 42
    assert prefs.chat(-1).auto_download is False and prefs.chat(-2).auto_download is True
    assert prefs.set_stats_opt_out(5, True).stats_opt_out
    assert prefs.user(6).stats_opt_out is False
    with pytest.raises(ValueError):
        prefs.update_chat(-1, bogus=5)
