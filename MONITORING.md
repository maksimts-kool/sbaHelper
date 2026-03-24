# Monitoring and logging

This project now includes:

- rotating file logs in `runtime/logs/`
- heartbeat files in `runtime/monitoring/`
- Docker healthchecks for all three services
- optional Sentry error reporting through `SENTRY_DSN`
- `.env` loading through `python-dotenv`

## Environment variables

- `LOG_LEVEL=INFO`
- `SENTRY_DSN=`
- `SENTRY_ENVIRONMENT=production`
- `SENTRY_TRACES_SAMPLE_RATE=0.0`
- `LOG_DIR=runtime/logs`
- `MONITORING_DIR=runtime/monitoring`

## Runtime files

- `runtime/logs/sbaradio-bot.log`
- `runtime/logs/sbaradio-tts.log`
- `runtime/logs/downloader-bot.log`
- `runtime/monitoring/sbaradio-bot.json`
- `runtime/monitoring/sbaradio-tts.json`
- `runtime/monitoring/downloader-bot.json`

## Healthchecks

Each container executes:

`python -m monitoring.healthcheck --service <service-name> --max-age 180`

If a heartbeat file becomes stale or a service reports `error`, Docker will mark the container as unhealthy.
