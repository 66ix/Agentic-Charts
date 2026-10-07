"""Test-suite environment: keep tests away from a developer's real caches, alert store and notification
channels, and from per-client rate limits (every test shares one app instance and client address)."""

import os

# A warm candle cache would change which requests the mocked-Binance tests see;
# test_candle_cache.py turns it back on with a tmp path.
os.environ["CANDLE_CACHE"] = "off"
os.environ["ALERTS_STORE"] = "memory"
os.environ["JOURNAL_STORE"] = "memory"
os.environ["GRIDBOTS_STORE"] = "memory"
os.environ["LLM_CHOICE_STORE"] = "memory"
os.environ["TRADES_STORE"] = "memory"
for _key in ("ALERT_HISTORY_STORE", "SIGNAL_ALERTS_STORE", "BRIEF_STORE"):
    os.environ[_key] = "memory"
for _key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
    os.environ[_key] = ""  # set (even empty) so python-dotenv will not fill it from .env
os.environ["AGENT_RATE_LIMIT"] = "0"
os.environ["API_RATE_LIMIT"] = "0"
