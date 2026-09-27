# SBA Helper

Telegram bot that downloads short vertical videos from **TikTok** and **YouTube Shorts**
right into the chat, and posts a weekly per-chat download summary.

## Run

```bash
cp .env.example .env        # set DOWNLOADER_BOT_TOKEN, ideally ADMIN_IDS and ALLOWED_CHAT_IDS
docker compose up --build -d
```

Without Docker (needs `ffmpeg` and `deno` on PATH, and `COOKIES_DIR` / `STATS_DB_PATH` pointed
somewhere writable):

```bash
uv sync
uv run --env-file .env -m sbahelper
```

All settings are described in [.env.example](.env.example).

## Deploy (Portainer)

CI publishes `ghcr.io/maksimts-kool/sbahelper:latest` after every green push to `main`, and
the Watchtower in [docker-compose.portainer.yml](docker-compose.portainer.yml) rolls it out
within the hour. Paste that file into Portainer → Stacks, set `DOWNLOADER_BOT_TOKEN`
(plus `ADMIN_IDS`, `ALLOWED_CHAT_IDS`) and create `/opt/sbahelper/cookies` on the host first.

## Cookies

As an admin, send the bot a cookie file in a private chat: a Netscape `cookies.txt` or the
JSON export of the Cookie-Editor extension, with any mix of sites. The bot keeps the TikTok and
YouTube cookies, saves them per platform and deletes your message. Dropping a file into the
cookies folder works too (imported on the next start). `/cookies` shows what is loaded, when
the login expires and whether the site accepted the cookies the last time they were used;
admins get a Telegram alert when a site asks for a login or cookies are about to expire.

YouTube cookies only last if nothing else uses that login: open a private/incognito window,
log in, open `https://www.youtube.com/robots.txt`, export the youtube.com cookies, then close
the window and never open that session again. A browser that keeps using the session rotates
the cookies and YouTube rejects the bot's copy within hours. The bot saves every rotation it
receives and, with `CHECK_YOUTUBE_URL` set, visits that video every `COOKIES_REFRESH_HOURS`
so the session stays in use. A spare Google account is safer than your main one.

## Commands

| Command    | Who    | What                                     |
| ---------- | ------ | ---------------------------------------- |
| `/start`   | anyone | What the bot does                        |
| `/stats`   | anyone | This week's downloads in this chat       |
| `/cookies` | admins | Cookie status                            |
| `/check`   | admins | Download the `CHECK_*_URL` test videos   |

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check && uv run pytest
```

See [CLAUDE.md](CLAUDE.md) for architecture and conventions.
