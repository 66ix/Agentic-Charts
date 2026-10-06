"""Runtime configuration, read once from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _proxy_hops(value: str) -> int:
    value = value.lower()
    if value in ("true", "on", "yes"):
        return 1
    return int(value) if value.isdigit() else 0


@dataclass(frozen=True)
class Settings:
    data_source: str = field(default_factory=lambda: _env("DATA_SOURCE", "auto").lower())
    binance_rest_url: str = field(default_factory=lambda: _env("BINANCE_REST_URL", "https://api.binance.com").rstrip("/"))
    binance_ws_url: str = field(default_factory=lambda: _env("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws").rstrip("/"))
    # Used automatically when the primary endpoints are geo-blocked (HTTP 451/403), e.g. from US IPs.
    binance_fallback_rest_url: str = field(
        default_factory=lambda: _env("BINANCE_FALLBACK_REST_URL", "https://data-api.binance.vision").rstrip("/"))
    binance_fallback_ws_url: str = field(
        default_factory=lambda: _env("BINANCE_FALLBACK_WS_URL", "wss://data-stream.binance.vision/ws").rstrip("/"))
    # USD-M futures: open interest and the liquidation stream for the header. Market streams moved
    # under /market/ws when Binance retired the legacy /ws path (2026-04-23).
    binance_futures_rest_url: str = field(
        default_factory=lambda: _env("BINANCE_FUTURES_REST_URL", "https://fapi.binance.com").rstrip("/"))
    binance_futures_ws_url: str = field(
        default_factory=lambda: _env("BINANCE_FUTURES_WS_URL", "wss://fstream.binance.com/market/ws").rstrip("/"))
    derivatives_enabled: bool = field(
        default_factory=lambda: _env("DERIVATIVES", "on").lower() not in ("off", "0", "false"))
    oi_top_symbols: int = field(default_factory=lambda: int(_env("OI_TOP_SYMBOLS", "40")))
    liquidations_store: str = field(default_factory=lambda: _env(
        "LIQUIDATIONS_STORE", str(Path(__file__).resolve().parent.parent / ".cache" / "liquidations.json")))

    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "ollama").lower())
    llm_timeout: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_SECONDS", "30")))
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "llama3.1:8b"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o-mini"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
    # "tools": the model may look at other timeframes/coins before planning (agent_loop.py);
    # "plan": one structured call, faster and better suited to small local models.
    agent_mode: str = field(default_factory=lambda: _env("AGENT_MODE", "tools").lower())
    agent_max_steps: int = field(default_factory=lambda: int(_env("AGENT_MAX_STEPS", "4")))
    # Rate limits per client ("N/second|minute|hour|day", or 0 to disable) and a global daily agent cap.
    agent_rate_limit: str = field(default_factory=lambda: _env("AGENT_RATE_LIMIT", "20/minute"))
    api_rate_limit: str = field(default_factory=lambda: _env("API_RATE_LIMIT", "300/minute"))
    agent_daily_limit: int = field(default_factory=lambda: int(_env("AGENT_DAILY_LIMIT", "0")))
    # How many reverse proxies sit in front of the API (0 = ignore X-Forwarded-For).
    trust_proxy: int = field(default_factory=lambda: _proxy_hops(_env("TRUST_PROXY", "0")))

    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip() for o in _env("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()
        )
    )
    metrics_cache_seconds: float = field(default_factory=lambda: float(_env("METRICS_CACHE_SECONDS", "60")))
    # Closed Binance candles kept in SQLite, so restarts only fetch the bars that are new.
    candle_cache: bool = field(
        default_factory=lambda: _env("CANDLE_CACHE", "on").lower() not in ("off", "0", "false"))
    candle_cache_path: str = field(default_factory=lambda: _env(
        "CANDLE_CACHE_PATH", str(Path(__file__).resolve().parent.parent / ".cache" / "candles.sqlite3")))

    # Kimi Cooked (app/kimi): closed candles it runs on. More candles = longer stats history, slower first run
    # (about 1.3 s per 5,000). TradingView's tables depend on how many candles the chart loaded too.
    kimi_bars: int = field(default_factory=lambda: max(300, min(int(_env("KIMI_BARS", "5000")), 5000)))

    # Server-side price alerts. ALERTS_STORE=memory keeps them in memory only (lost on restart).
    alerts_store: str = field(default_factory=lambda: _env(
        "ALERTS_STORE", str(Path(__file__).resolve().parent.parent / ".cache" / "alerts.json")))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", ""), repr=False)
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))
    discord_webhook_url: str = field(default_factory=lambda: _env("DISCORD_WEBHOOK_URL", ""), repr=False)

    # Trade journal (journal.py): logged trades. JOURNAL_STORE=memory keeps them in memory only (lost on restart).
    journal_store: str = field(default_factory=lambda: _env(
        "JOURNAL_STORE", str(Path(__file__).resolve().parent.parent / ".cache" / "journal.json")))


@lru_cache
def get_settings() -> Settings:
    return Settings()
