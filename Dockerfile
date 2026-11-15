# Build the virtualenv with uv; only the venv reaches the final image.
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

FROM python:3.14-slim

# ffmpeg/ffprobe for merging and media checks, tzdata for TZ, CA certificates for TLS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime for YouTube and picks deno up automatically.
COPY --from=denoland/deno:bin-2.9.7 /deno /usr/local/bin/deno
COPY --from=build /opt/venv /opt/venv

# A fixed, unprivileged identity. Volumes must be owned by this UID/GID
# (README: "Non-root volume migration").
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --system --gid "${APP_GID}" sbahelper \
    && useradd --system --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
       --home-dir /tmp --shell /usr/sbin/nologin sbahelper \
    && install -d -o "${APP_UID}" -g "${APP_GID}" -m 0700 /data /data/cookies

# /tmp is the only writable path besides /data when the root filesystem is
# read-only: work directories, the heartbeat and the yt-dlp/deno caches.
ENV TZ=Europe/Tallinn \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/cache \
    DENO_DIR=/tmp/cache/deno \
    TEMP_DIR=/tmp \
    HEARTBEAT_PATH=/tmp/sbahelper.heartbeat

WORKDIR /app
COPY sbahelper ./sbahelper
RUN python -m compileall -q sbahelper

USER ${APP_UID}:${APP_GID}
VOLUME /data
STOPSIGNAL SIGTERM
# Liveness: the bot rewrites the heartbeat every 30 s. No Telegram call, no download.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "sbahelper.health"]
CMD ["python", "-m", "sbahelper"]
