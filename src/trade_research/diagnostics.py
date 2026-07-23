"""Secret-free, side-effect-bounded local deployment diagnostics."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trade_research.providers import ProviderConfigurationError
from trade_research.settings import Settings

JsonObject = dict[str, Any]


def run_doctor(environment: Mapping[str, str] | None = None) -> JsonObject:
    """Return capability states only; never return configuration values or paths."""

    env = os.environ if environment is None else environment
    data_directory = Path(env.get("TRADE_RESEARCH_DATA_DIR", ".trade-research"))
    writable = _probe_writable(data_directory)
    wal = _probe_sqlite_wal(data_directory) if writable else False
    config_file = env.get("TRADE_RESEARCH_CONFIG")
    supported = (3, 12) <= sys.version_info[:2] < (3, 14)
    provider_configured = _configured(
        env,
        "TRADE_RESEARCH_PROVIDER",
        "TRADE_RESEARCH_PRICE_PROVIDER",
        "TRADE_RESEARCH_FUNDAMENTAL_PROVIDER",
        "TRADE_RESEARCH_CONFIG",
    )
    try:
        settings = Settings.from_environment(env)
    except ProviderConfigurationError:
        settings = None
    prices_available = bool(settings and settings.price_provider)
    fundamentals_available = bool(settings and settings.fundamental_provider)
    filings_available = bool(settings and settings.sec_user_agent)
    provider_valid = settings is not None and prices_available and fundamentals_available
    checks_ok = supported and writable and wal and provider_valid
    return {
        "research_only": True,
        "status": "ok" if checks_ok else "warning",
        "python": {
            "supported": supported,
            "implementation": sys.implementation.name,
            "version": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
        "configuration": {
            "data_directory_configured": "TRADE_RESEARCH_DATA_DIR" in env,
            "config_file_configured": config_file is not None,
            "config_file_readable": bool(config_file and Path(config_file).is_file()),
            "api_token_configured": (
                (settings is not None and settings.api_token is not None)
                or _configured(env, "TRADE_RESEARCH_API_TOKEN", "TRADE_RESEARCH_API_TOKEN_FILE")
            ),
        },
        "storage": {"writable": writable, "sqlite_wal": wal},
        "providers": {
            "configured": provider_configured,
            "valid": provider_valid,
            "prices_available": prices_available,
            "fundamentals_available": fundamentals_available,
            "filings_available": filings_available,
            "connectivity": "not_attempted",
        },
        "skills": {
            "technical_analysis": (
                "ready" if prices_available else
                "unavailable — price provider not configured"
            ),
            "fundamental_analysis": (
                "ready" if fundamentals_available else
                "unavailable — fundamental provider not configured"
            ),
            "filings_analysis": (
                "ready" if filings_available else
                "unavailable — SEC user agent not configured"
            ),
            "combined_analysis": (
                "ready"
                if prices_available and fundamentals_available and filings_available
                else "unavailable"
            ),
            "worth_buy_stocks_analysis": (
                "ready" if prices_available else
                "unavailable — price provider not configured"
            ),
            "markov_method_analysis": (
                "ready" if prices_available else
                "unavailable — price provider not configured"
            ),
        },
        "llm": {
            "configured": _configured(
                env,
                "TRADE_RESEARCH_LLM_API_KEY",
                "TRADE_RESEARCH_LLM_API_KEY_FILE",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
            ),
            "connectivity": "not_attempted",
        },
        "discord": {
            "configured": (
                (settings is not None and settings.discord_webhook_url is not None)
                or _configured(env, "DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL_FILE")
            ),
            "connectivity": "not_attempted",
        },
    }


def _configured(environment: Mapping[str, str], *names: str) -> bool:
    return any(bool(environment.get(name)) for name in names)


def _probe_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory):
            pass
    except OSError:
        return False
    return True


def _probe_sqlite_wal(directory: Path) -> bool:
    database: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".sqlite3", delete=False) as handle:
            database = Path(handle.name)
        with sqlite3.connect(database) as connection:
            row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            enabled = bool(row and str(row[0]).lower() == "wal")
    except (OSError, sqlite3.Error):
        return False
    finally:
        if database is not None:
            for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
                candidate.unlink(missing_ok=True)
    return enabled
