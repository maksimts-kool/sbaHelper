# CLAUDE.md

Guidance for working on this repository.

## What this is

`sbahelper` is a Telegram bot (python-telegram-bot 22, long polling) for a few group chats.
When a message contains a TikTok or YouTube link (or someone uses `/download`), it downloads
the video with yt-dlp and replies with it, but only **short vertical** videos (up to
`MAX_SHORT_DURATION_SEC`, up to `MAX_FILE_SIZE_BYTES`). Sent videos are cached by Telegram
`file_id`, logged to SQLite for statistics, and each chat gets a weekly summary. Bot admins
get error alerts in Telegram DMs and manage cookies by sending the bot a file.

All user-facing text is **Russian only** (no translations, no language settings). Code,
comments, logs and docs are **English**.

## Commands

```bash
uv sync                                  # install (Python 3.14, deps pinned in uv.lock)
uv run pytest                            # offline, ~5 s; media tests use ffmpeg if present
uv run ruff check && uv run ruff format --check
uv run --env-file .env -m sbahelper      # run locally (needs ffmpeg, ffprobe, deno on PATH)
docker compose up --build -d             # run locally in Docker (state in ./data, UID 10001)
uv run python -m sbahelper.health --deps # tools + yt-dlp private API check
uv run python -m sbahelper.backup create # consistent online DB backup
uv lock --upgrade                        # bump dependencies, then run the tests
```

CI (`.github/workflows/ci.yml`): `test` (ruff, pytest with real ffmpeg), `audit`
(pip-audit on the locked runtime deps), `container` (build, non-root/read-only startup,
health, migration, compose validation). A push that passes all three publishes
`ghcr.io/maksimts-kool/sbahelper:<sha>` plus `:latest` (main) or `:branch-<name>` (other
branches; not Dependabot, not pull requests). Actions are pinned to commit
SHAs; Dependabot (`.github/dependabot.yml`) bumps uv packages, base images and actions
weekly. **Nothing deploys automatically** (Watchtower was removed): production runs the tag
set in the Portainer stack (`SBAHELPER_TAG`). See docs/OPERATIONS.md.

## Layout

```text
sbahelper/
  __main__.py   python -m sbahelper → app.main()
  app.py        ApplicationBuilder wiring, post_init/post_stop/post_shutdown, main()
  config.py     Settings from env (single source of defaults); access policy; byte limits
  log.py        One-line log format; every line goes through redact.redact()
  redact.py     Masks bot tokens, cookie/auth headers, secret pairs, URL query values
  alerts.py     logging.Handler: ERROR records → admins' DMs (grouped, bounded, redacted)
  pacing.py     PTB rate limiter: per-chat/global buckets, RetryAfter, LOW priority
  links.py      URL validation, entity extraction (message_links), cache identity (Link.key)
  download.py   yt-dlp engine: selectors, impersonation, validation, error codes, Cancel
  media.py      ffprobe inspection, MP4 faststart remux (no shell, timeouts)
  cookies.py    Cookie parsing/import, per-run session copies, merge-back, health
  checks.py     Smoke checks (CHECK_*_URL); last result kept in the DB state table
  db.py         SQLite connections, versioned migrations, online backup
  stats.py      StatsStore over db: record/aggregate periods, opt-out, delete, migrate chat
  cache.py      FileCache: Telegram file_id per (bot id, video key), URL aliases
  prefs.py      Chat settings (auto-download, summary topic, active) and user opt-out
  service.py    DownloadService: admission, fair queue, dedup, workers, delivery, shutdown
  runtime.py    Runtime dataclass in bot_data["runtime"] (service, db, stores, pacer)
  texts.py      EVERY user-facing string and renderer; Telegram length limits
  handlers.py   Thin Telegram handlers; all authorization checks live here
  jobs.py       Weekly summary, daily maintenance, cookie expiry/refresh, heartbeat
  health.py     Healthcheck CLI (heartbeat file) and --deps self-check
  backup.py     Backup CLI (create / verify)
tests/          pytest, one file per module; fakes.py has Telegram and downloader fakes
docs/OPERATIONS.md  deployment, non-root migration, backup/restore, updates, troubleshooting
```

Dependency direction: `config`/`log`/`redact`/`texts` ← `links`/`cookies`/`media`/`db` ←
`download` ← `stats`/`cache`/`prefs`/`checks` ← `service` ← `runtime` ← `handlers` ← `jobs` ←
`app`. `texts` imports model types only under `TYPE_CHECKING`.

## How a link is handled

1. `handlers.on_message` (passive; needs the chat allowed and its auto-download on) or
   `handlers.cmd_download` (also as a reply). `links.message_links` reads text, caption and
   `url`/`text_link` entities (PTB handles UTF-16 offsets), validates each URL (https, a
   TikTok/YouTube host, no userinfo/odd port), drops profile/listing URLs, dedups by
   identity. At most `MAX_LINKS_PER_MESSAGE` are taken.
2. `service.admit()` checks shutdown, per-user rate, per-user/per-chat pending, queue
   capacity and free temp space, and returns a `Ticket` (or `Refused(code)`). The handler
   replies `texts.CHECKING` and calls `service.submit()`.
3. `submit` checks the file cache (directly for `youtube:<id>`/`tiktok:<id>`, via an alias
   for short links). Hit → sent by `file_id`. Else the request joins an in-flight `Job` for
   the same key or a new job is queued (per-chat deques, round-robin).
4. A job runs `download.download_video` in a ThreadPoolExecutor of `MAX_ACTIVE_DOWNLOADS`.
   `download_video` makes a marked work dir, opens a private cookie copy, **extracts once**
   (`_extract`), validates metadata (`validate`: playlist → other site → live → photo →
   orientation → duration → size estimate), calls `on_info` (which may raise `Satisfied` on
   a cache hit under the resolved id), downloads **from the same YoutubeDL and info dict**,
   then `check_media` (ffprobe: video stream, TikTok audio, real orientation/duration,
   truncation, faststart remux) and the final byte check.
5. Delivery: first waiter uploads (`reply_video`, quoting the link; topics are kept by
   PTB's reply shortcuts), its `file_id` is cached, other waiters get the file by id. A
   failed upload makes the next waiter upload again. The job deletes the work dir after the
   last waiter. Each delivery (cached or not) records a `DownloadEvent`.

Cancellation is cooperative: `download.Cancel` (event + deadline) is checked between steps,
in the progress hook (raising `_Abort`, a `DownloadCancelled`) and during retry back-off.
A worker that overruns `DOWNLOAD_TIMEOUT_SEC + OVERRUN_GRACE_SEC` is reported to waiters
as timed out but **keeps its slot until the thread ends**; its late result is cleaned up.
Never wrap `asyncio.to_thread` in a timeout and assume the work stopped.

### Errors

`download.DownloadError` has `code` (selects the Russian text in `texts.error_text`),
`reason` (redacted, for logs and /check), `params` (sizes, durations) and `alert`.
Subclasses: `Rejected` (codes photo/profile/live/horizontal/too_long/too_big/unsupported;
flashed 5 s, INFO), `Unavailable`, `PrivateVideo`, `LoginRequired` (alert; the text must
not claim the cookies *are* expired), `Blocked` (alert), `NetworkFailure`, `ToolMissing`
(alert), `MediaError` (incl. `no_audio`), `TimedOut`, `Cancelled`. `user_error()` maps
yt-dlp messages by substring tokens. No delayed retries; `_extract` only rotates
impersonation targets and retries network errors up to 3 times.

## Telegram rules we follow

- Size: one byte limit everywhere. Selectors use **plain byte numbers** (yt-dlp reads
  `50M` as 10^6 and `50MiB` as 2^20). Ceiling 50,000,000 (cloud Bot API); default 45 MB.
- Text: captions ≤ 1024, messages ≤ 4096 visible UTF-16 units. Shorten plain text with
  `texts.truncate` *before* `safe()`; `texts.fit()` guarantees the total. Never slice HTML.
- Pacing: everything goes through `pacing.Pacer`. Pass `rate_limit_args=pacing.LOW` for
  droppable calls (progress edits, chat actions) and handle `Throttled`. Only `RetryAfter`
  is retried; never retry a timed-out send.
- Callbacks: data matches `handlers._CALLBACK` (≤ 64 bytes), is re-authorized on press, and
  every query is answered exactly once.
- Updates: `allowed_updates` = message, callback_query, my_chat_member. Removal/ban marks a
  chat inactive (no summaries); `Forbidden` on send does too. Group→supergroup migration
  moves statistics and settings.

## Logging conventions

Format (see `log.py`): `2026-09-23T00:07:06+03:00 [DOWNLOAD] INFO: Downloaded. Platform=tiktok Size=3100000`

- Tag = logger name (`sbahelper.<module>` → `<MODULE>`; app/handlers → `BOT`). Always
  `log = logging.getLogger(__name__)`.
- Message = short past-tense sentence + `Key=Value` pairs, `%s` args, no f-strings.
  Correlate with `Req=` (request) and `Job=` ids. Do not log display names or cookie values.
- Everything is redacted on output (`redact.redact`), but still prefer `redact.safe_url()`
  for URLs you log on purpose.
- **ERROR means "an admin should look"**: it becomes a Telegram DM. User-caused or
  transient failures are WARNING or lower. Records logged while sending an alert are not
  alerted again.

## Cookies

- One Netscape file per platform in `COOKIES_DIR` (0700; files 0600 via `cookies.secure`).
- Ways in: admin DM (`handlers.on_cookie_file`, ≤ 1 MB by advertised *and* actual size;
  unknown size is refused; the message is deleted and the reply says if that failed) or a
  dropped `*.txt`/`*.json` imported at startup. An import **replaces** that platform's file.
- `cookies.session()` yields a `CookieSession(path, platform, generation)`: a private copy;
  `_merge_back` merges only that run's changes under `_lock`. Imports bump `_generation`;
  a run on older cookies merges nothing **and its health verdict is dropped**
  (`record(..., generation=)`). Never point yt-dlp at the real file.
- `jobs.refresh_cookies` visits each `CHECK_*_URL` every `COOKIES_REFRESH_HOURS`; it helps
  keep sessions alive but guarantees nothing.

## yt-dlp specifics (read before touching download.py)

- Impersonation: `_YoutubeDL` overrides the private `_parse_impersonate_targets`;
  `impersonate_targets()` uses `_impersonate_target_available`. `tests/test_ytdlp_compat.py`
  and `health --deps` check both against the installed yt-dlp.
- `allowed_extractors` = youtube*, tiktok*, vm.tiktok: the generic extractor never runs.
- Selectors cap the **width** (`[width<=?1080]` = up to 1080×1920). TikTok prefers single
  H.264 files with audio; YouTube the best H.264 under `limit - 5 MB`.
- YouTube needs deno (in the image; `DENO_DIR`/`XDG_CACHE_HOME` point into /tmp because
  the root filesystem is read-only).
- Only `download.py` imports yt_dlp (plus the compat tests and `health`).

## Database

`db.Database`: WAL, `busy_timeout`, short-lived connections, explicit `transaction()`
(`BEGIN IMMEDIATE`). Never hold a transaction across network or download work.
Migrations live in `db.MIGRATIONS`; each runs with its `user_version` bump in one
transaction. **To change the schema, append a migration**; never edit an applied one.
Existing data gets a pre-migration backup automatically. v1 = legacy `downloads` (adopted
with `IF NOT EXISTS` + `username` guard), v2 = `video_cache` + `url_aliases`,
v3 = `chat_settings`, `user_settings`, `state`. Keep old rows readable (legacy `facebook`
rows still render). Cache rows hold no user data. Retention (stats) and cache expiry are
separate and pruned by the daily `maintenance` job in batches.

## Stats

Calendar periods in `TZ` from their start to now (`stats.period_bounds`); intervals are
`[start, end)`. The weekly job runs on `STATS_WEEKLY_WEEKDAY` (Monday = 0) at
`STATS_WEEKLY_TIME`; JobQueue counts days from Sunday, hence `(weekday + 1) % 7`. Inactive
and disallowed chats get nothing. Opted-out users are stored as `user_id = 0`.

## Configuration

`config.Settings.from_env()` is the single source of defaults; compose files only pass
variables through. Invalid values fall back with a warning (`settings.issues`), except the
allowlist, which fails closed. To add a setting: field in `Settings`, read in `from_env`,
add to `.env.example`, `docker-compose.portainer.yml` (if settable in Portainer) and
`tests/conftest.py`.

| Variable | Default | Notes |
| --- | --- | --- |
| `DOWNLOADER_BOT_TOKEN` | (none) | required |
| `ACCESS_POLICY` | open if no allowlist, else allowlist | open / allowlist; invalid → allowlist |
| `ALLOWED_CHAT_IDS` | empty | invalid entries dropped, never widens access |
| `ADMIN_IDS` | empty | alerts, cookies, /status, /cookies, /check (private chat) |
| `TZ` / `LOG_LEVEL` | Europe/Tallinn / INFO | |
| `MAX_FILE_SIZE_BYTES` | 45000000 | 1e6…5e7; `MAX_FILE_SIZE_MB` deprecated (×10^6) |
| `MAX_SHORT_DURATION_SEC` | 300 | |
| `MAX_LINKS_PER_MESSAGE` | 3 | |
| `MAX_ACTIVE_DOWNLOADS` / `MAX_QUEUED_DOWNLOADS` | 3 / 20 | |
| `MAX_PENDING_PER_USER` / `MAX_PENDING_PER_CHAT` | 3 / 8 | |
| `USER_LINKS_PER_MINUTE` | 10 | |
| `DOWNLOAD_TIMEOUT_SEC` | 240 | |
| `TEMP_DIR` | system temp (/tmp) | work dirs `sbahelper-dl-*` with a marker file |
| `CACHE_TTL_DAYS` | 30 | 0 = no cache |
| `DROP_PENDING_UPDATES` | 1 | |
| `COOKIES_DIR` | /data/cookies | |
| `CHECK_TIKTOK_URL`, `CHECK_YOUTUBE_URL` | empty | |
| `COOKIES_REFRESH_HOURS` | 4 | 0 = off |
| `STATS_ENABLED` | 1 | |
| `STATS_DB_PATH` | /data/downloader_stats.db | the whole database, not only stats |
| `STATS_WEEKLY_WEEKDAY` / `STATS_WEEKLY_TIME` | 6 / 20:00 | |
| `STATS_RETENTION_DAYS` | 400 | 0 = forever |
| `HEARTBEAT_PATH` | /tmp/sbahelper.heartbeat | |

## Deployment notes

- Image: `python:3.14-slim` + Debian `ffmpeg` + `ca-certificates` + `deno` + a uv-built venv.
  Runs as **UID/GID 10001**, read-only root, `/tmp` tmpfs, caps dropped, healthcheck on the
  heartbeat. `/data` holds the database (and cookies locally). In Portainer, cookies are a
  host bind mount (`COOKIES_HOST_DIR`, default `/opt/sbahelper/cookies`) owned by 10001.
- Volumes from the old root image must be chowned once: docs/OPERATIONS.md.
- Keep the service and container name `downloader-bot`, and one instance per token.
- GHCR packages are private by default: make `sbahelper` public or add a `read:packages` PAT.

## Testing conventions

- pytest functions, no network. `conftest.py` resets `settings` (monkeypatch) and gives each
  test its own cookie dir, temp dir, DB path and heartbeat path. Patch `settings` fields,
  not env vars (except config tests).
- Fakes in `tests/fakes.py` (`FakeMessage`, `FakeRunner`, `drain`). Real PTB objects only
  where library behaviour is the point (entities, topic replies, JobQueue scheduling).
- Async code runs through `asyncio.run(...)` inside plain test functions.
- Never monkeypatch `asyncio.sleep` or `time.monotonic` globally: inject a clock/sleep
  (`Pacer(clock=, sleep=)`, `DownloadService(clock=)`).
