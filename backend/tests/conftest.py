"""Keep the test suite away from a developer's real alert store and notification channels
(backend/.cache/alerts.json, TELEGRAM_* / DISCORD_WEBHOOK_URL in backend/.env)."""

import os

os.environ["ALERTS_STORE"] = "memory"
for _key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
    os.environ[_key] = ""  # set (even empty) so python-dotenv will not fill it from .env
