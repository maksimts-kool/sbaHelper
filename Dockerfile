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

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime for YouTube and picks deno up automatically.
COPY --from=denoland/deno:bin-2.9.7 /deno /usr/local/bin/deno
COPY --from=build /opt/venv /opt/venv

ENV TZ=Europe/Tallinn \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY sbahelper ./sbahelper

VOLUME /data
CMD ["python", "-m", "sbahelper"]
