# CLAUDE.md

Guidance for working on this repository.

## What this is

`sbahelper` is a Telegram bot (python-telegram-bot 22, polling) for a few group chats. When a
message contains a TikTok or YouTube link, it downloads the video with yt-dlp and replies with
it, but only for **short vertical** videos (Shorts / TikToks up to `MAX_SHORT_DURATION_SEC`,
up to `MAX_FILE_SIZE_MB`). Every sent video is logged to SQLite, and once a week each chat gets
a summary (leaderboard, platforms, most-viewed video). Admins get error alerts in Telegram DMs
and manage cookies by sending the bot a file.

All user-facing text is **Russian**. Code, comments, logs and docs are **English**.

## Commands

```bash
uv sync                                  # install (Python 3.14, deps pinned in uv.lock)
uv run pytest                            # tests: offline, no ffmpeg/deno needed, <1 s
uv run ruff check && uv run ruff format --check
uv run --env-file .env -m sbahelper      # run locally (needs ffmpeg + deno on PATH)
docker compose up --build -d             # run locally in Docker (state in ./data)
uv lock --upgrade                        # bump dependencies, then run the tests
```

CI (`.github/workflows/ci.yml`) runs ruff check, ruff format --check and pytest; on `main` it
then builds and pushes `ghcr.io/maksimts-kool/sbahelper:{latest,<sha>}`. Production
(Portainer, `docker-compose.portainer.yml`) runs that image and Watchtower redeploys hourly, so
**a green push to main ships to production**.

## Layout

```text
sbahelper/
  __main__.py   python -m sbahelper → app.main()
  app.py        ApplicationBuilder wiring, post_init (alerts, stats, jobs, cookies, menus), main()
  config.py     Settings dataclass read once from env; `settings` global; Env readers collect issues
  log.py        Pretty one-line log format + setup_logging()
  alerts.py     logging.Handler that DMs ERROR records to ADMIN_IDS (replaces Sentry)
  links.py      Link detection (find_link), profile/listing filter, platform_of()
  download.py   yt-dlp engine: selectors, impersonation, validation, errors, download_video()
  cookies.py    Cookie parsing (Netscape + JSON), import/split per platform, status, session copies
  checks.py     Smoke checks: download CHECK_*_URL videos (startup + /check)
  stats.py      SQLite StatsStore + weekly aggregate(); pure data, no Telegram
  texts.py      EVERY user-facing string and renderer (captions, status, weekly summary, admin)
  handlers.py   Telegram handlers: links, /start, /stats, /cookies, /check, cookie upload, errors
  jobs.py       JobQueue jobs: weekly summary, daily cookie-expiry check, startup checks
tests/          pytest, one file per module; conftest.py resets `settings` for every test
```

Dependency direction: `config`/`log`/`texts` ← `links`/`cookies`/`stats` ← `download` ←
`checks` ← `handlers` ← `jobs` ← `app`. `texts` imports model types only under
`TYPE_CHECKING` because `download` imports `texts` for its error messages.

## How a link is handled

1. `handlers.on_message`: chat allowed? `links.find_link` finds the first TikTok/YouTube URL;
   profile/channel/playlist URLs (`is_not_a_video`) are ignored silently.
2. Reply with a status message (`texts.CHECKING`) and run `download.download_video` in a worker
   thread (`asyncio.to_thread`). Up to 8 updates are processed concurrently.
3. `download_video` makes a temp dir, opens a private cookie copy (`cookies.session`), then
   **extracts once** (`_extract`) and validates the metadata (`validate`): playlist → live →
   photo post → orientation → duration. It calls `on_info`, then downloads **from the same
   YoutubeDL instance and info dict** (`process_ie_result`). One extraction per link means
   fewer TikTok requests, and TikTok ties video URLs to cookies set during extraction.
4. After the download: pick the output file, check TikTok audio (ffprobe) and the size limit.
5. The handler edits the status (title + progress bar, at most one edit per second, never
   after a final text), replies with the video (`reply_video`, quoting the link, width/height/
   duration set), deletes the status, records a `DownloadEvent`, and removes the temp dir.

### Errors

`download.DownloadError` has two fields: `text` is HTML shown in chat, and `reason` is a plain
string for logs and `/check`. Subclasses:

- `Rejected`: the link works but the content is not a short vertical video (photo, live,
  profile, horizontal, too long, too big). Shown for 5 s, then deleted. Logged at INFO.
- `LoginRequired` (`alert = True`): the site wants a login, so cookies are missing or expired.
  Logged at ERROR with "Send fresh cookies…".
- `Blocked` (`alert = True`): TikTok rejected every impersonation target.
- Plain `DownloadError`: network errors (after quick retries) and anything else from yt-dlp.
  Logged at WARNING and left in chat.

`download.user_error()` maps yt-dlp messages to these by substring tokens (`_BLOCKED_ERRORS`,
`_LOGIN_ERRORS`, `_NETWORK_ERRORS`). There is **no delayed retry** (the old 2-minute retry and
its pickle persistence were removed on purpose). The only retries are in `_extract`:
impersonation-target rotation and up to 3 fast retries on network errors.

## Logging conventions

Format (see `log.py`): `2026-09-23T00:07:06+03:00 [DOWNLOAD] INFO: Downloaded. Platform=tiktok Size=3.1MB`

- Tag = logger name: `sbahelper.<module>` → `<MODULE>` (aliases: app/handlers → `BOT`,
  httpx → `HTTP`, apscheduler → `SCHEDULER`). Always use `log = logging.getLogger(__name__)`.
- Message = short past-tense sentence + `Key=Value` pairs: `"Video sent. Chat=%s User=%s"`.
  Use `%s` args, not f-strings. `_who(chat_id, user)` builds `Chat=… User=…`.
- Timestamps use `TZ` (default Europe/Tallinn). Colours only on a TTY / `FORCE_COLOR`.
- **ERROR means "an admin should look"**: every ERROR record becomes a Telegram DM via
  `alerts.TelegramAlerts`. User-caused or transient failures must be WARNING or lower. Alerts
  are grouped by message with numbers and URLs masked, and each group is sent at most once
  per 10 minutes.
- yt-dlp is quiet and its own messages go to the `yt_dlp` logger at DEBUG; we log outcomes
  ourselves.

## Cookies

- One Netscape file per platform: `COOKIES_DIR/tiktok.txt`, `COOKIES_DIR/youtube.txt`
  (`PLATFORM_DOMAINS` decides which cookie goes where; YouTube also takes google.com).
- Ways in: an admin DMs a file (`handlers.on_cookie_file` → `cookies.import_text`; the message
  is deleted afterwards), or any other `*.txt`/`*.json` in the folder is imported on startup
  (`import_dropped`) and renamed `*.imported`. Formats: Netscape and Cookie-Editor /
  EditThisCookie JSON. An import **replaces** that platform's file.
- `cookies.session()` hands yt-dlp a private copy and `os.replace`s it back after a successful
  run, so refreshed cookies persist. It skips the swap if the file changed meanwhile (new
  upload). Never point yt-dlp at the real file: yt-dlp rewrites its cookie file in place on
  close, which is not safe with parallel downloads.
- `cookies.status()` reads login cookies (`LOGIN_COOKIES`) for `/cookies`, the startup log and
  the daily `jobs.check_cookie_expiry` (ERROR → alert when <3 days left or expired).

## yt-dlp specifics (read before touching download.py)

- **Impersonation:** TikTok needs curl-cffi. Its extractor asks for "any" target, yt-dlp would
  choose the newest one, and TikTok's WAF blocks exactly that one. `_YoutubeDL` overrides the
  private `_parse_impersonate_targets` to use `IMPERSONATE_TARGETS` (newest known-good first),
  and `_extract` moves down the list on a block. When everything is blocked, check
  `yt-dlp --list-impersonate-targets` and update the tuple. `impersonate_targets()` also uses
  the private `_impersonate_target_available`, so re-test both after yt-dlp upgrades.
- **Format selectors:** only vertical videos get through, so the resolution cap is on
  **width** (`[width<=?1080]` means up to 1080×1920). `height<=1080` would cap Shorts at
  480×854. TikTok prefers single H.264 files that already contain audio (merged TikTok tracks
  can be silent; a silent TikTok is rejected after the ffprobe check). YouTube prefers the best
  H.264 that fits `MAX_FILE_SIZE_MB - 5` via `filesize_approx`, then any codec, then 720p.
- **YouTube** needs a JS runtime: the Docker image ships `deno`, which yt-dlp finds on PATH
  automatically (`yt-dlp[default]` bundles the EJS solver scripts). Locally, install deno.
- Only `download.py` imports yt_dlp. Tests replace `_extract` / `_YoutubeDL` with fakes.

## Texts

- Everything a chat can see lives in `texts.py`: constants for fixed strings, small functions
  for parametrised ones, plus the caption/status/weekly/admin renderers. To change wording,
  edit only this file.
- Parse mode is **HTML** everywhere (set once through `Defaults` in `app.py`). Custom emoji use
  `<tg-emoji emoji-id=…>` (`texts._emoji`).
- Anything from users or sites goes through `texts.safe()`: HTML-escape plus `defuse()`, which
  inserts WORD JOINERs so titles and names cannot become links, @mentions, #hashtags or
  clickable times. `@username` in the leaderboard is left live on purpose, as a real mention.
- Invisible names (HANGUL FILLER etc.) are detected by `has_visible_text` and shown as
  "Участник <id>". Keep such characters as `\uXXXX` escapes in source, never literally.

## Stats

`stats.StatsStore` keeps one row per sent video in `downloads` (SQLite, a new connection per
call, so it is safe from threads). `aggregate()` builds `WeeklyStats`, and
`texts.weekly_stats()` renders it. The week runs from Monday 00:00 in `TZ` up to "now". The
weekly job runs on `STATS_WEEKLY_WEEKDAY` (Monday = 0) at `STATS_WEEKLY_TIME`; JobQueue counts
days from Sunday, hence `(weekday + 1) % 7`. Chats without downloads, and chats outside
`ALLOWED_CHAT_IDS`, get nothing. Rows older than `STATS_RETENTION_DAYS` are pruned after each
run. The schema only grows: add columns with an `ALTER TABLE` guard like `username` in
`StatsStore.__init__`, and keep old rows readable (legacy `facebook` rows still render).

## Configuration

`config.Settings.from_env()` is the single source of defaults. The compose files only pass
variables through, with empty values meaning "default". Invalid values fall back to the
default and are logged as warnings at startup (`settings.issues`); they never crash the bot.
To add a setting: add a field to `Settings`, read it in `from_env` with the right `Env` reader,
then add it to `.env.example`, to `docker-compose.portainer.yml` if it should be settable in
Portainer, and to `tests/conftest.py` if tests depend on it.

| Variable | Default | Notes |
| --- | --- | --- |
| `DOWNLOADER_BOT_TOKEN` | (none) | required |
| `ALLOWED_CHAT_IDS` | empty = all | comma-separated |
| `ADMIN_IDS` | empty | alerts, cookie upload, /cookies, /check |
| `TZ` | Europe/Tallinn | logs + weekly summary |
| `LOG_LEVEL` | INFO | |
| `MAX_FILE_SIZE_MB` | 50 | Telegram bot upload limit |
| `MAX_SHORT_DURATION_SEC` | 300 | |
| `COOKIES_DIR` | /data/cookies | |
| `CHECK_TIKTOK_URL`, `CHECK_YOUTUBE_URL` | empty | smoke-check videos |
| `STATS_ENABLED` | 1 | |
| `STATS_DB_PATH` | /data/downloader_stats.db | |
| `STATS_WEEKLY_WEEKDAY` / `STATS_WEEKLY_TIME` | 6 / 20:00 | |
| `STATS_RETENTION_DAYS` | 400 | 0 = keep forever |

## Deployment notes

- Image: `python:3.14-slim` + Debian `ffmpeg` + `deno` binary + a venv built by uv in a
  separate stage (uv is not in the final image). `/data` is the only state (stats DB and, in
  local compose, cookies). In Portainer, cookies are a host bind mount at `/data/cookies`
  (`COOKIES_HOST_DIR`, default `/opt/sbahelper/cookies`); create it before deploying.
- Keep the service and container name `downloader-bot`: renaming it in Portainer leaves the
  old container running next to the new one.
- GHCR packages are private by default. Either make `sbahelper` public or add a ghcr.io
  registry with a `read:packages` PAT in Portainer.

## Testing conventions

- pytest functions, no network. `conftest.py` resets `settings` fields (monkeypatch) and gives
  each test its own `cookies_dir`/`stats_db_path` under `tmp_path`. Patch `settings` fields
  with `monkeypatch.setattr(settings, ...)`, not env vars.
- Telegram objects are small fakes (`tests/test_handlers.py::FakeMessage`); only `test_jobs.py`
  builds a real `Application` (for JobQueue scheduling).
- Async code runs through `asyncio.run(...)` inside plain test functions.
- Do not monkeypatch `time.monotonic` or `asyncio.sleep` globally without keeping a reference to
  the original: the event loop uses them too.
