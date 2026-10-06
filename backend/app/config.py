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


@dataclass(frozen=True)
class Settings:
    data_source: str = field(default_factory=lambda: _env("DATA_SOURCE", "auto").lower())
    binance_rest_url: str = field(default_factory=lambda: _env("BINANCE_REST_URL", "https://api.binance.com").rstrip("/"))
    binance_ws_url: str = field(default_factory=lambda: _env("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws").rstrip("/"))

    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "ollama").lower())
    llm_timeout: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_SECONDS", "30")))
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "llama3.1:8b"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o-mini"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-5-5"))

    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip() for o in _env("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()
        )
    )
    metrics_cache_seconds: float = field(default_factory=lambda: float(_env("METRICS_CACHE_SECONDS", "60")))


@lru_cache
def get_settings() -> Settings:
    return Settings()
