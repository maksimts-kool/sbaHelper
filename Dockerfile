FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg tzdata && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Tallinn

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all python files (main.py, playlist_service.py, time_service.py)
COPY *.py ./

CMD ["python", "main.py"]