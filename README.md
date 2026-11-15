# SBA Helper

Telegram bot that downloads short vertical videos from **TikTok** and **YouTube Shorts**
right into the chat, and posts a weekly per-chat download summary. The bot speaks Russian.

Send a link → get the video. Long, horizontal, photo and live posts are politely refused.

## Features

- Links in messages and captions, including hidden `text_link` targets; up to
  `MAX_LINKS_PER_MESSAGE` per message, duplicates counted once.
- `/download <link>` (or `/download` as a reply) works even when Telegram does not show
  the bot ordinary group messages (Privacy Mode).
- One download per video at a time: people sending the same link share it, and every later
  request is answered instantly from Telegram's file cache.
- A fair, bounded queue with per-user/per-chat limits, a rate limit, disk-space checks and
  a hard time budget per download.
- Media checks on the real file (ffprobe): video stream present, real duration and size,
  vertical, not truncated, index moved to the front for streaming.
- Statistics per day, week and month (`/stats`), a weekly summary (optionally into a forum
  topic), personal opt-out and deletion.
- Bot-admin tools in private chat: `/status`, `/cookies`, `/check`, cookie upload; error
  alerts in DM with secrets redacted.
- Non-root, read-only container with a liveness healthcheck; versioned SQLite migrations
  with automatic pre-migration backups.

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/start`, `/help` | anyone | What the bot does, all commands |
| `/download <link>` | anyone | Download explicitly; reply with it to a message that has a link |
| `/stats [day\|week\|month]` | anyone | Chat statistics, with buttons to switch the period |
| `/settings` | anyone | Private chat: your statistics opt-out. Group: auto-download and summary topic (group admins only) |
| `/privacy` | anyone | What is stored and for how long |
| `/delete_my_data` | anyone | Delete your statistics in every chat and your settings (asks first) |
| `/status` | bot admins | Uptime, queue, cache counters, free space, database, cookies, last checks, access policy |
| `/cookies` | bot admins | Cookie status |
| `/check` | bot admins | Download the `CHECK_*_URL` test videos |

Admins also send cookie files to the bot in a private chat. Command menus are only a
convenience: every handler checks permissions itself.

## Run

```bash
cp .env.example .env        # set DOWNLOADER_BOT_TOKEN, ideally ADMIN_IDS and ALLOWED_CHAT_IDS
mkdir -p data/cookies       # on Linux also: sudo chown -R 10001:10001 data
docker compose up --build -d
```

Without Docker (needs `ffmpeg`, `ffprobe` and `deno` on PATH, and `COOKIES_DIR` /
`STATS_DB_PATH` pointed somewhere writable):

```bash
uv sync
uv run --env-file .env -m sbahelper
```

All settings, with defaults, are in [.env.example](.env.example). Production deployment,
volume migration, backups, updates and rollback: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Access, Privacy Mode and roles

Three independent gates decide whether the bot reacts:

1. **Telegram Privacy Mode** decides which group messages the bot *receives*. With it on
   (the default), a bot gets commands, replies to its own messages and service messages,
   and everything only if it is a group administrator
   ([Privacy Mode](https://core.telegram.org/bots/features#privacy-mode),
   [what messages bots get](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get)).
   For automatic link detection, turn it off in @BotFather (Bot Settings → Group Privacy),
   then **remove and re-add the bot** to existing groups, or make the bot an admin (no
   rights needed). If you would rather not, `/download` always works. `/status` shows the
   bot-wide setting; it cannot tell which groups have re-added the bot.
2. **The bot's allowlist** (`ACCESS_POLICY`, `ALLOWED_CHAT_IDS`) decides which chats it
   *serves*. A malformed allowlist fails closed. The effective policy is logged at startup.
3. **Roles**: bot admins (`ADMIN_IDS`) run the admin commands in private; a group's
   Telegram administrators change that group's settings; everyone changes only their own
   personal settings. Being a bot admin gives no rights over groups.

Group auto-download can be switched off per chat in `/settings`; the bot then answers only
`/download`.

## Limits and units

- `MAX_FILE_SIZE_BYTES` (default 45,000,000) is the one limit used for format selection,
  stopping a transfer that grows too big, and the final check. The cloud Bot API documents
  50 MB for uploads ([sendVideo](https://core.telegram.org/bots/api#sendvideo)); the 5 MB
  headroom is this project's choice. Values above 50,000,000 are refused; a local Bot API
  server is not supported. Sizes shown in chat are decimal (1 МБ = 1,000,000 bytes).
- Captions are kept within 1024 and messages within 4096 characters after entity parsing;
  long titles and names are shortened before escaping.
- Outbound messages are paced per chat (about 1/s in private chats, 20/min in groups) and
  globally (25/s), following the [Bot FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this);
  flood-control errors are retried after Telegram's `retry_after`. Progress edits are
  skipped rather than delayed when a chat is busy. Timeouts are never retried blindly: an
  upload that timed out may have arrived, and the user is told so.

## Cache

After a successful upload the bot stores Telegram's `file_id` for the video
(`tiktok:<id>` / `youtube:<id>`), namespaced by the bot's numeric id (file ids do not
transfer between bots) and by the size/length rules. The next request for the same video
is sent by `file_id` without downloading. Short links (vm.tiktok.com/…) are remembered
after the first resolution. Entries expire after `CACHE_TTL_DAYS`; an id Telegram rejects
is dropped and the video downloaded afresh once. Cache rows contain no user data.

Two *different* short links to the same video arriving at the same moment are downloaded
twice (their identity is only known after extraction); later requests hit the cache.

## Cookies

As an admin, send the bot a cookie file in a private chat: a Netscape `cookies.txt` or the
JSON export of the Cookie-Editor extension, with any mix of sites (up to 1 MB). The bot
keeps the TikTok and YouTube cookies, saves them per platform (folder 0700, files 0600) and
deletes your message; if Telegram does not let it delete the message, it tells you to do it
yourself. Deleting a message removes it from the chat; it cannot recall copies elsewhere.
Dropping a file into the cookies folder works too (imported on the next start).

`/cookies` shows what is loaded, when the login expires and whether the site accepted the
cookies the last time they were used. When a site asks for a login the admins get an alert:
expired or revoked cookies are the usual cause, but the site may also be asking for other
reasons.

YouTube cookies only last if nothing else uses that login: open a private/incognito window,
log in, open `https://www.youtube.com/robots.txt`, export the youtube.com cookies, then close
the window and never open that session again. With `CHECK_YOUTUBE_URL` set the bot visits
that video every `COOKIES_REFRESH_HOURS` so the session stays in use; this helps but does
not guarantee YouTube keeps accepting it. A spare Google account is safer than your main one.

## Statistics and privacy

One row per video sent: chat, user id, name, @username, platform, title, author, length,
size, views, time. Kept `STATS_RETENTION_DAYS` (default 400), pruned daily. Periods are
calendar periods in `TZ` from their start until now (today, this week from Monday, this
month from the 1st); boundaries follow local midnight across DST changes. The weekly
summary covers Monday 00:00 until it is sent.

- `/settings` → opt out: your future videos are counted without your id or name.
- `/delete_my_data` deletes your rows in every chat and your settings, matched by your
  Telegram user id. Chat totals shrink accordingly. Messages already sent, logs (chat/user
  ids and links, rotated by Docker) and database backups are not touched.
- Video files are deleted from the server right after sending.

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check && uv run pytest
```

Tests are offline. Media tests use real ffmpeg when installed and skip otherwise;
`tests/test_ytdlp_compat.py` exercises the installed yt-dlp against a local HTTP server;
`tests/test_lifecycle.py` starts the real bot against a fake Bot API server and stops it
with SIGTERM. See [CLAUDE.md](CLAUDE.md) for architecture and conventions.

Dependabot opens weekly PRs for uv packages, base images and pinned GitHub Actions. For an
urgent extractor fix: `uv lock --upgrade-package yt-dlp`, run the tests, merge.
