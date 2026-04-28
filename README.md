# SBA Helper Bots

This repo runs two Telegram bots from one Docker Compose project:

- `downloader-bot` downloads supported videos from YouTube, TikTok, and Facebook.
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
│   ├── app/service.py       # Bot, uMap client, checks, state, formatters
│   └── app/tests.py         # Unit tests
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
- `UMAP_MAP_ID`
- `UMAP_LAYER_ID`
- `UMAP_PLANS_LAYER_ID`, if you want to watch the plans layer too

Put cookie files in `downloader/cookies/`. `COOKIES_FILE` is used for TikTok/Facebook. If YouTube needs cookies, set `YOUTUBE_COOKIES_FILE` to a separate YouTube cookie file.

The uMap bot stores its state in one JSON file at `UMAP_STATE_PATH=/data/umap-state.json`, persisted in the `umap-data` Docker volume.

## Commands

```powershell
docker compose up --build downloader-bot
docker compose --profile umap up --build umap-route-bot

docker compose --profile check run --rm --build downloader-check
docker compose --profile check run --rm --build umap-check

docker compose --profile test run --rm --build umap-test
docker compose run --rm downloader-bot python -m downloader.service check
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

It stores known route IDs, route snapshots, subscribers, message IDs, and check timestamps in the JSON state file.

## Error Tracking

Both bots support optional Sentry error tracking through:

- `SENTRY_DSN`
- `SENTRY_ENVIRONMENT`
- `SENTRY_RELEASE`
