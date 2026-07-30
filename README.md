# SBA Helper Bot

`downloader-bot` is a Telegram bot that downloads short vertical videos from
YouTube Shorts and TikTok, and posts a weekly download summary to each group it
works in.

## Structure

```text
.
├── shared.py                # Logging, transient-error detection, Sentry, startup printing
├── downloader/              # yt-dlp downloader bot (python-telegram-bot)
│   ├── config.py            # Settings, platform/URL detection, checks, Sentry
│   ├── download.py          # yt-dlp engine: models, options, metadata, download
│   ├── stats.py             # SQLite download log + weekly aggregation
│   ├── formatting.py        # Telegram caption/status/statistics formatting
│   ├── bot.py               # Telegram app, message flow, progress, jobs, entrypoint
│   └── cookies/             # Local cookie files, ignored by Git
├── tests/
│   ├── conftest.py          # Stubs the runtime deps so tests need no network/binaries
│   ├── test_config.py
│   ├── test_download.py
│   ├── test_formatting.py
│   └── test_stats.py
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml           # ruff + pytest config
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

Run the bot with `python -m downloader.bot`.

## Setup

There are two compose files:

- `docker-compose.yml` — local development. Builds from the `Dockerfile`, keeps
  state in `./data`, reads `.env` (copy it from `.env.example`).
- `docker-compose.portainer.yml` — production. Pulls the published image, keeps
  state in named volumes, reads Portainer stack environment variables. See
  [Portainer stack](#portainer-stack).

Both map every variable explicitly, so a missing `/data/compose/.../.env` file
is never required.

The example surfaces the values a deployment usually tunes:

- `DOWNLOADER_BOT_TOKEN` — the bot token (required).
- `MAX_FILE_SIZE_MB`, `MAX_SHORT_DURATION_SEC` — limits.
- `STATS_WEEKLY_WEEKDAY`, `STATS_WEEKLY_TIME`, `STATS_TIMEZONE` — when the
  weekly summary is posted.
- `ALLOWED_CHAT_IDS`, `COOKIES_FILE`, `YOUTUBE_COOKIES_FILE`, `SENTRY_DSN` —
  optional.

Everything else (Sentry environment/release, log level, startup-check
strictness, the optional `CHECK_*` smoke-check URLs, `STATS_DB_PATH`,
`STATS_ENABLED`, `STATS_RETENTION_DAYS`) has a default in `downloader/config.py`
and is already listed in both compose files for stack overrides.

Put cookie files in `downloader/cookies/`. `COOKIES_FILE` is used for TikTok.
If YouTube needs cookies, set `YOUTUBE_COOKIES_FILE` to a separate YouTube
cookie file.

## Commands

Build and start the bot:

```powershell
docker compose up --build -d
```

The `./data` directory holds the statistics SQLite file and is created on first
run.

## Startup checks

Before it starts polling Telegram, the container downloads a test video for
each configured platform and throws it away. This runs the same code path a
real request does — format selection, yt-dlp, ffmpeg merge, the TikTok
audio-track check, the size limit — so a broken extractor, expired cookies or a
missing ffmpeg surfaces at deploy time instead of on someone's first link:

```text
OK youtube: Clip by Author (28s, 4.2 MB)
OK tiktok: Cat by user (14s, 2.1 MB)
```

Point `CHECK_YOUTUBE_URL` and `CHECK_TIKTOK_URL` at stable short vertical
videos. Unset checks are skipped, so leaving both empty disables startup
checking. Pick videos unlikely to be deleted or geo-blocked — a dead fixture
fails every restart.

- `STARTUP_CHECK_DOWNLOAD=0` falls back to metadata-only checks (~2s instead of
  ~10-20s), which verifies the extractor and cookies but never the download.
- `STARTUP_CHECKS_REQUIRED=1` makes a hard failure stop startup. Two things are
  always warnings rather than blockers: a fixture that is simply wrong for this
  bot (photo post, video too long), and flaky provider responses such as a
  TikTok 403. A file that downloads but exceeds `MAX_FILE_SIZE_MB` *does* block
  — that means the format selector picked something Telegram cannot accept.

Telegram commands:

- `/start` — what the bot does and which links it accepts
- `/stats` — this week's summary so far, for the current chat

## Weekly statistics

Every successfully sent video is logged as one row in a SQLite file
(`STATS_DB_PATH`, default `/data/downloader_stats.db`): timestamp, chat, user
(id, name and `@username` if they have one), platform, duration, file size,
title, uploader, and view count. Nothing else is stored — rejected links and
errors are not logged.

Once a week the bot posts a per-chat summary. Each group sees only its own
downloads, and groups with no downloads that week get no message:

```text
📊 Итоги недели · 20–26 июля

Скачано: 47 видео · 1 ч 12 мин · 812 МБ

🏆 Кто больше всех
🥇 @maksim — 21
🥈 Аня — 14
🥉 Пётр — 8
   ещё 3 участника — 4

📱 Откуда
▏▓▓▓▓▓▓▓░░░  |  TikTok — 33
▏▓▓▓░░░░░░░  |  YouTube — 14

🔥 Хит недели
«Кот открывает холодильник» — 2.4M 👁
```

The leaderboard prefers a real `@username` mention, so the entry always reads
the way that person is currently signed in Telegram — and pings them. Members
without a username fall back to the name recorded at download time, with `@`
defused so a display name cannot ping anyone. "📱 Откуда" is a quote, which is
not monospace: the bar leads each line precisely because all bars are the same
width and line up without padding.

Defaults: Sunday at 20:00 `Europe/Tallinn`, covering that week from Monday
00:00 up to the moment the message is sent. Downloads made after the Sunday
send time fall outside both that report and the next one, which starts at
Monday 00:00 — move `STATS_WEEKLY_TIME` later, or `STATS_WEEKLY_WEEKDAY` to `0`
(Monday), if that tail matters.

- `STATS_WEEKLY_WEEKDAY` uses `datetime.weekday()` numbering: `0` = Monday,
  `6` = Sunday.
- `STATS_ENABLED=0` turns logging, `/stats`, and the weekly job off.
- `STATS_RETENTION_DAYS` (default `400`) prunes older rows after each weekly
  run; `0` keeps everything.
- "🔥 Хит недели" is shown only when at least one video that week reported a
  view count.

## Portainer stack

Production runs from the published image, not from a build.

**1. Publish the image.** `.github/workflows/ci.yml` builds and pushes
`ghcr.io/maksimts-kool/sbahelper` on every green push to `main`. The `publish`
job is gated on `lint-and-test`, so a red commit is never deployed.

**2. Make the package pullable.** GHCR packages are **private by default**, and
a private package makes the stack fail with `denied` / `manifest unknown`. After
the first successful publish, either:

- GitHub → Packages → `sbahelper` → Package settings → Change visibility →
  Public; or
- keep it private and add a GHCR registry in Portainer (Registries → Custom
  registry, `ghcr.io`, your username, a PAT with `read:packages`).

**3. Create the host cookie directory** before deploying, otherwise Docker
creates it root-owned and empty:

```bash
mkdir -p /opt/sbahelper/cookies
# put cookies.txt here (TikTok); YouTube cookies only if needed
```

**4. Deploy.** Portainer → Stacks → Add stack → Web editor, paste
`docker-compose.portainer.yml`, and set the stack environment variables:

| Variable | Required | Notes |
| --- | --- | --- |
| `DOWNLOADER_BOT_TOKEN` | yes | Deploy fails fast if unset |
| `ALLOWED_CHAT_IDS` | no | Comma-separated; empty means the bot answers everywhere |
| `COOKIES_HOST_DIR` | no | Host cookie directory, default `/opt/sbahelper/cookies` |
| `STATS_WEEKLY_WEEKDAY` / `STATS_WEEKLY_TIME` / `STATS_TIMEZONE` | no | Weekly summary schedule |
| `SENTRY_DSN` | no | Error tracking |

Everything else falls back to the defaults baked into the compose file.

Two Portainer-specific details in that file: state lives in **named volumes**
(`stats-data`, `download-tmp`) rather than paths relative to the stack folder,
and Watchtower runs **without a compose profile** — Portainer's UI cannot select
profiles, so a profiled service would silently never start.

### Automatic updates (Watchtower)

[`nickfedor/watchtower`](https://github.com/nickfedor/watchtower) (a maintained,
label-compatible fork of the archived `containrrr/watchtower`) ships in the
Portainer stack and checks hourly for a new `:latest`, pulling and restarting
`downloader-bot` when it finds one. Only containers labelled
`com.centurylinklabs.watchtower.enable=true` are touched.

In the local `docker-compose.yml` it sits behind a `watchtower` profile
(`docker compose --profile watchtower up -d`) and is a no-op there, because
Watchtower only updates registry-pulled images and local dev builds from source.

## Development

```powershell
pip install -r requirements-dev.txt
ruff check .
ruff format --check .
pytest
```

CI (`.github/workflows/ci.yml`) runs ruff lint, ruff format check, and pytest on
push/PR. All three must pass.

## Error Tracking

The bot supports optional Sentry error tracking through:

- `SENTRY_DSN`
- `SENTRY_ENVIRONMENT`
- `SENTRY_RELEASE`
