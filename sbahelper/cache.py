"""Telegram file cache: send a video Telegram already has instead of downloading it again.

After a successful upload Telegram returns a `file_id`, which this bot can
reuse to send the same file again (https://core.telegram.org/bots/api#sending-files).
File ids belong to one bot and are not transferable, so entries are keyed by
the bot's numeric id (never the token) plus the video identity
(`tiktok:<id>` / `youtube:<id>`). `file_unique_id` cannot be used to send and
is not stored. Telegram does not promise file ids live forever
(https://core.telegram.org/bots/faq#can-i-count-on-file-ids-to-be-persistent):
entries expire after CACHE_TTL_DAYS, and an id Telegram rejects is deleted and
the video downloaded afresh.

`policy` is `download.MEDIA_POLICY_VERSION` combined with the size and length
limits, so a file cached under other rules is not reused. Failures are never
cached. Rows hold video metadata only, nothing about who asked.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sbahelper.db import Database, utc_text
from sbahelper.download import VideoInfo

MEDIA_KINDS = ("video", "animation", "document")


@dataclass(frozen=True, slots=True)
class CachedVideo:
    video_key: str
    file_id: str
    media_kind: str
    info: VideoInfo
    size_bytes: int


def policy_fingerprint(version: int, max_bytes: int, max_duration: int) -> int:
    digest = hashlib.sha256(f"{version}:{max_bytes}:{max_duration}".encode()).digest()
    return int.from_bytes(digest[:6], "big")


class FileCache:
    def __init__(self, db: Database, *, bot_id: int, policy: int, ttl_days: int) -> None:
        self.db = db
        self.bot_id = bot_id
        self.policy = policy
        self.ttl = timedelta(days=ttl_days)

    @property
    def enabled(self) -> bool:
        return self.ttl > timedelta(0)

    def _fresh_after(self) -> str:
        return utc_text(datetime.now(UTC) - self.ttl)

    def resolve(self, url_key: str) -> str | None:
        """The video a short link pointed to last time, if remembered."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT video_key FROM url_aliases WHERE url_key = ? AND created_at >= ?",
                (url_key, self._fresh_after()),
            ).fetchone()
        return str(row[0]) if row else None

    def remember_alias(self, url_key: str, video_key: str) -> None:
        if not url_key.startswith("url:") or url_key == video_key:
            return
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO url_aliases (url_key, video_key, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(url_key) DO UPDATE SET video_key = excluded.video_key, "
                "created_at = excluded.created_at",
                (url_key, video_key, utc_text()),
            )

    def get(self, video_key: str) -> CachedVideo | None:
        if not self.enabled:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT file_id, media_kind, platform, title, uploader, duration_sec, width, "
                "height, view_count, like_count, size_bytes FROM video_cache "
                "WHERE bot_id = ? AND video_key = ? AND policy = ? AND created_at >= ?",
                (self.bot_id, video_key, self.policy, self._fresh_after()),
            ).fetchone()
        if row is None:
            return None
        file_id, kind, platform, title, uploader, duration, width, height, views, likes, size = row
        video_id = video_key.split(":", 1)[1] if ":" in video_key else None
        info = VideoInfo(
            platform=str(platform),
            title=str(title),
            uploader=str(uploader),
            duration=int(duration),
            view_count=views,
            like_count=likes,
            width=width,
            height=height,
            video_id=video_id,
        )
        return CachedVideo(video_key, str(file_id), str(kind), info, int(size))

    def touch(self, video_key: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE video_cache SET last_used_at = ? WHERE bot_id = ? AND video_key = ?",
                (utc_text(), self.bot_id, video_key),
            )

    def put(
        self, video_key: str, file_id: str, media_kind: str, info: VideoInfo, size: int
    ) -> None:
        if not self.enabled or media_kind not in MEDIA_KINDS or not file_id:
            return
        now = utc_text()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO video_cache (bot_id, video_key, policy, file_id, media_kind, "
                "platform, "
                "title, uploader, duration_sec, width, height, view_count, like_count, size_bytes, "
                "created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(bot_id, video_key) DO UPDATE SET policy = excluded.policy, "
                "file_id = excluded.file_id, media_kind = excluded.media_kind, "
                "title = excluded.title, uploader = excluded.uploader, "
                "duration_sec = excluded.duration_sec, width = excluded.width, "
                "height = excluded.height, view_count = excluded.view_count, "
                "like_count = excluded.like_count, size_bytes = excluded.size_bytes, "
                "created_at = excluded.created_at, last_used_at = excluded.last_used_at",
                (
                    self.bot_id,
                    video_key,
                    self.policy,
                    file_id,
                    media_kind,
                    info.platform,
                    info.title,
                    info.uploader,
                    info.duration,
                    info.width,
                    info.height,
                    info.view_count,
                    info.like_count,
                    size,
                    now,
                    now,
                ),
            )

    def invalidate(self, video_key: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM video_cache WHERE bot_id = ? AND video_key = ?",
                (self.bot_id, video_key),
            )

    def count(self) -> int:
        with self.db.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM video_cache WHERE bot_id = ?", (self.bot_id,)
                ).fetchone()[0]
            )

    def prune(self) -> int:
        """Drop expired entries and aliases (all bots: expired is expired)."""
        cutoff = self._fresh_after() if self.enabled else utc_text(datetime.now(UTC) + timedelta(1))
        removed = self.db.prune_in_batches(
            "SELECT rowid FROM video_cache WHERE created_at < ?", "video_cache", (cutoff,)
        )
        removed += self.db.prune_in_batches(
            "SELECT rowid FROM url_aliases WHERE created_at < ?", "url_aliases", (cutoff,)
        )
        return removed
