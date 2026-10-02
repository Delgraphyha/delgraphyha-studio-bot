# Delgraphyha Studio Bot - Render Free Docker

This package runs the Telegram bot, Flask health endpoint, and a local bgutil PO-token provider inside one Render Web Service.

Render settings:
- Language/Runtime: Docker
- Plan: Free
- Health Check Path: /health
- Keep existing environment variables, especially TELEGRAM_TOKEN and YOUTUBE_COOKIES.
- Docker CMD starts `python main.py`; no separate Start Command is required.

The PO-token provider binds only to 127.0.0.1:4416 inside the container.
