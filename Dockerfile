FROM node:24-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        python3 \
        python3-pip \
        python3-venv \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Tallinn
ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN python3 -m venv "$VIRTUAL_ENV"

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY downloader ./downloader
COPY umap ./umap

CMD ["python", "-m", "downloader.service"]
