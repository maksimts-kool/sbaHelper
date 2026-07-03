# SBA Helper Bots

This repo runs two Telegram bots from one Docker Compose project:

- `downloader-bot` downloads supported videos from YouTube Shorts, TikTok, and Facebook.
- `umap-route-bot` watches uMap route layers and sends Telegram notifications for new or changed routes.

## Structure

```text
.
├── shared.py                # Logging, transient-error detection, Sentry, startup printing
├── downloader/              # yt-dlp downloader bot (python-telegram-bot)
│   ├── config.py            # Settings, platform/URL detection, checks, Sentry
│   ├── download.py          # yt-dlp engine: models, options, metadata, download
│   ├── formatting.py        # Telegram caption/status formatting
│   ├── bot.py               # Telegram app, message flow, progress, entrypoint
│   └── cookies/             # Local cookie files, ignored by Git
├── umap/                    # aiogram uMap route watcher
│   ├── config.py            # Env settings, watched layers, route models, Sentry
│   ├── formatting.py        # Bike/plans/walk/change Telegram formatting
│   ├── service.py           # uMap HTTP client, SQLite state store, watcher service
│   └── bot.py               # Startup checks, polling loops, commands, entrypoint
├── tests/
│   ├── test_downloader.py
│   └── test_umap.py
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml           # ruff + mypy + pytest config
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

Run the packages with `python -m downloader.bot` and `python -m umap.bot`.

## Setup

For local Docker Compose, create `.env` from `.env.example`. For Portainer or
other stack deployers, set the same keys as stack environment variables instead
of relying on a physical `.env` file. `docker-compose.yml` maps those variables
explicitly, so a missing `/data/compose/.../.env` file is not required.

The example surfaces the values a deployment usually tunes:

- `DOWNLOADER_BOT_TOKEN` and `TELEGRAM_BOT_TOKEN` — the two bot tokens (required).
- `UMAP_BIKE_MAP_ID`, `UMAP_BIKE_LAYER_ID`, `UMAP_BIKE_PLANS_LAYER_ID`,
  `UMAP_WALK_MAP_ID` — the maps/layers to watch. The bike map/layer default in
  code; set `UMAP_WALK_MAP_ID` to also watch walking routes (the bot fetches
  every datalayer in that map and adds the source vald/layer title to each
  notification).
- `MAX_FILE_SIZE_MB`, `MAX_SHORT_DURATION_SEC`, `POLL_INTERVAL_SECONDS`,
  `CHANGE_POLL_INTERVAL_SECONDS` — limits and intervals.
- `ALLOWED_CHAT_IDS`, `DEFAULT_SUBSCRIBER_CHAT_ID`, `COOKIES_FILE`,
  `YOUTUBE_COOKIES_FILE`, `SENTRY_DSN` — optional.

Everything else (Sentry environment/release, request timeouts/retries, log level,
startup-check strictness, the optional `CHECK_*` smoke-check URLs, `UMAP_BASE_URL`,
`UMAP_STATE_DB`, etc.) has a default in `*/config.py` and is already listed in
`docker-compose.yml` for stack overrides.

Put cookie files in `downloader/cookies/`. `COOKIES_FILE` is used for
TikTok/Facebook. If YouTube needs cookies, set `YOUTUBE_COOKIES_FILE` to a
separate YouTube cookie file.

The uMap bot stores its whole state as one JSON document in a SQLite file. By
default that file is `/data/umap_state.db`, mounted from `./data` (override with
`UMAP_STATE_DB`). No external database is required.

## Commands

Build and start both bots:

```powershell
docker compose up --build -d
```

The `./data` directory holds the uMap SQLite state file and is created on first
run.

The downloader container runs the (now optional) link checks before polling
Telegram. Configure `CHECK_YOUTUBE_URL` / `CHECK_TIKTOK_URL` / `CHECK_FACEBOOK_URL`
to enable them; unset checks are skipped. With `STARTUP_CHECKS_REQUIRED=1`,
hard check failures still stop startup, while known unsupported content and flaky
provider responses are reported as warnings.

The uMap container runs uMap endpoint checks before polling Telegram. With
`UMAP_STARTUP_CHECKS_REQUIRED=1`, failed checks stop startup.

## Automatic updates (Watchtower)

[`nickfedor/watchtower`](https://github.com/nickfedor/watchtower) (a maintained,
label-compatible fork of the archived `containrrr/watchtower`) is included under a
`watchtower` compose profile:

```powershell
docker compose --profile watchtower up -d
```

Watchtower only updates **registry-pulled images**. For production auto-updates,
push a tagged image and uncomment the `image:` line on `downloader-bot` (and set
one on `umap-route-bot`); with `build: .` Watchtower is a no-op.

## uMap Bot

The uMap bot supports:

- `/start` - show available commands
- `/subscribe` - subscribe the current chat
- `/unsubscribe` - unsubscribe the current chat
- `/status` - show layer and subscriber status
- `/check` - run an immediate new-route check
- `/chatid` - show the current chat id
- `/testnotify` - send a test route notification

It stores known route IDs, route snapshots, subscribers, message IDs, and check
timestamps as one JSON document in the SQLite state file.

Bike/plans notifications use the default formatter; walking notifications are
formatted separately in `umap/formatting.py`:

- Walking route details come from structured fields like `there_1_from`,
  `there_1_to`, `there_1_minutes`, `there_1_route`, and matching `back_*` fields.
  The legacy `Instruction` placeholder format is still supported while data is
  migrated.
- Walking features with a checked `planned` property use the same planned-route
  notification titles/emojis as the bike plans layer.

## Development

```powershell
pip install -r requirements-dev.txt
ruff check .
ruff format --check .
pytest
```

CI (`.github/workflows/ci.yml`) runs ruff lint, ruff format check, and pytest on
push/PR; mypy runs informationally.

## Error Tracking

Both bots support optional Sentry error tracking through:

- `SENTRY_DSN`
- `SENTRY_ENVIRONMENT`
- `SENTRY_RELEASE`
