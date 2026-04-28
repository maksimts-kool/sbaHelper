# Downloader Bot

Telegram bot for downloading supported videos from YouTube, TikTok, and Facebook.

## Structure

```text
.
├── downloader/              # Python application package
│   ├── bot.py               # Telegram bot setup
│   ├── check_links.py       # Service smoke checks
│   ├── config.py            # Environment configuration
│   ├── core.py              # yt-dlp download/fetch logic
│   ├── entrypoint.py        # Startup checks, then bot launch
│   ├── error_tracking.py    # Optional Sentry integration
│   ├── handlers.py          # Telegram message handlers
│   ├── url_support.py       # Supported URL detection
│   └── cookies/             # Local cookie files, ignored by Git
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Setup

Create `.env` from `.env.example`, fill in `DOWNLOADER_BOT_TOKEN`, `ALLOWED_CHAT_IDS`, and the three `CHECK_*_URL` values.

Put cookie files in `downloader/cookies/`. `COOKIES_FILE` is used for TikTok/Facebook. If YouTube needs cookies, set `YOUTUBE_COOKIES_FILE` to a separate YouTube cookie file.

## Commands

```powershell
docker compose up --build downloader-bot
docker compose --profile check run --rm --build downloader-check
docker compose run --rm downloader-bot python -m downloader.check_links
```

The bot container runs the three link checks before polling Telegram. With `STARTUP_CHECKS_REQUIRED=1`, failed checks stop startup.
