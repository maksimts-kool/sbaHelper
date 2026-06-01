# SBA Helper Bots

This repo runs two Telegram bots from one Docker Compose project:

- `downloader-bot` downloads supported videos from YouTube Shorts, TikTok, and Facebook.
- `umap-route-bot` watches uMap route layers and sends Telegram notifications for new or changed routes.

## Structure

```text
.
├── downloader/              # Python application package
│   ├── service.py           # Settings, supported links, checks, startup, Sentry
│   ├── bot.py               # Telegram bot setup
│   ├── handlers.py          # Telegram message flow
│   ├── core.py              # yt-dlp download/fetch logic
│   └── cookies/             # Local cookie files, ignored by Git
├── umap/                    # Plain Python + aiogram uMap route watcher
│   ├── watcher.py           # Telegram bot entrypoint, runtime, and polling loops
│   ├── settings.py          # Environment-driven uMap layer configuration
│   ├── models.py            # Route dataclasses, GeoJSON extraction, geometry/hash helpers
│   ├── state.py             # MongoDB-backed state store
│   ├── client.py            # uMap HTTP client
│   ├── checks.py            # Startup checks
│   ├── errors.py            # Sentry and transient error handling
│   ├── formatting.py        # Bike/plans/change Telegram formatting
│   └── walk/formatter.py    # Walking route notification formatting
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Setup

Create `.env` from `.env.example`.

For the downloader bot, fill in:

- `DOWNLOADER_BOT_TOKEN`
- `ALLOWED_CHAT_IDS`
- `CHECK_YOUTUBE_URL`
- `CHECK_TIKTOK_URL`
- `CHECK_FACEBOOK_URL`

For the uMap route bot, fill in:

- `TELEGRAM_BOT_TOKEN`
- `DEFAULT_SUBSCRIBER_CHAT_ID`, if you want one chat subscribed on first boot
- `UMAP_STATE_MONGODB_URI`
- `UMAP_BIKE_MAP_ID` and `UMAP_BIKE_LAYER_ID`
- `UMAP_BIKE_PLANS_LAYER_ID`, if you want to watch the bike plans layer too
- `UMAP_WALK_MAP_ID`, if you want to watch walking routes. The bot fetches every datalayer in that map and adds the source vald/layer title to each notification.

Map links are built from `UMAP_BIKE_MAP_ID` and `UMAP_WALK_MAP_ID`.

Put cookie files in `downloader/cookies/`. `COOKIES_FILE` is used for TikTok/Facebook. If YouTube needs cookies, set `YOUTUBE_COOKIES_FILE` to a separate YouTube cookie file.

The uMap bot stores its state only in MongoDB. `UMAP_STATE_MONGODB_URI` is required.

## Commands

For local development, use the local compose file. It starts MongoDB in the
same project and stores data in the `mongodb-data` Docker volume, so you do not
need to set `UMAP_STATE_MONGODB_URI` unless you want to point at another
database:

```powershell
docker compose -f docker-compose.local.yml up --build -d
```

The default `docker-compose.yml` is intended for the Portainer deployment where
MongoDB is already available on the external `shared-db` network.

```powershell
docker compose up --build -d
```

The bot container runs the three link checks before polling Telegram. With `STARTUP_CHECKS_REQUIRED=1`, failed checks stop startup.

The uMap container runs uMap endpoint checks before polling Telegram. With `UMAP_STARTUP_CHECKS_REQUIRED=1`, failed checks stop startup.

## uMap Bot

The uMap bot supports:

- `/start` - show available commands
- `/subscribe` - subscribe the current chat
- `/unsubscribe` - unsubscribe the current chat
- `/status` - show layer and subscriber status
- `/check` - run an immediate new-route check
- `/chatid` - show the current chat id
- `/testnotify` - send a test route notification

It stores known route IDs, route snapshots, subscribers, message IDs, and check timestamps in the `UMAP_STATE_MONGODB_COLLECTION` collection as one document.

Bike/plans notifications use the default formatter in `umap/formatting.py`.
Walking notifications are formatted separately:

- `umap/walk/formatter.py` handles walking route details from structured fields like `there_1_from`, `there_1_to`, `there_1_minutes`, `there_1_route`, and matching `back_*` fields. It still supports the legacy `Instruction` placeholder format while the data is being migrated.
- Walking features with a checked `planned` property use the same planned-route notification titles/emojis as the bike plans layer.

## Error Tracking

Both bots support optional Sentry error tracking through:

- `SENTRY_DSN`
- `SENTRY_ENVIRONMENT`
- `SENTRY_RELEASE`
