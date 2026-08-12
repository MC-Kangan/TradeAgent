"""Typed allowlisted application configuration loaded without secret persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, model_validator

from trade_research.providers.contracts import ProviderConfigurationError

PriceProviderName = Literal["local_csv", "local_parquet", "local_sql", "yahoo", "ccxt", "bloomberg"]
FundamentalProviderName = Literal["local_csv", "local_parquet", "local_sql", "sec_company_facts"]
MAX_CONFIG_BYTES: Final = 64 * 1024


class Settings(BaseModel):
    """Closed production composition settings; arbitrary keys are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    price_provider: PriceProviderName | None = None
    price_path: Path | None = None
    fundamental_provider: FundamentalProviderName | None = None
    fundamental_path: Path | None = None
    ccxt_exchange: str | None = None
    bloomberg_host: str = "localhost"
    bloomberg_port: int = 8194
    data_root: Path | None = None
    api_token: SecretStr | None = None
    discord_webhook_url: SecretStr | None = None
    sec_user_agent: str | None = None
    sec_cik_map: dict[str, str] = {}
    sec_cik_overrides: dict[str, str] = {}

    @model_validator(mode="after")
    def validate_paths(self) -> Settings:
        if self.price_provider in {"local_csv", "local_parquet", "local_sql"}:
            if self.price_path is None:
                raise ValueError("local price provider requires price_path")
            _validate_local_path(self.price_provider, self.price_path)
            _validate_path_containment(self.data_root, self.price_path)
        elif self.price_path is not None:
            raise ValueError("price_path is accepted only for local price providers")
        if (
            self.fundamental_provider is not None
            and self.fundamental_provider != "sec_company_facts"
            and self.fundamental_path is None
        ):
            raise ValueError("local fundamental provider requires fundamental_path")
        if self.fundamental_provider is not None and self.fundamental_path is not None:
            _validate_local_path(self.fundamental_provider, self.fundamental_path)
            _validate_path_containment(self.data_root, self.fundamental_path)
        if self.fundamental_provider is None and self.fundamental_path is not None:
            raise ValueError("fundamental_path requires a fundamental_provider")
        if self.fundamental_provider == "sec_company_facts" and not self.sec_user_agent:
            raise ValueError("sec_company_facts provider requires sec_user_agent")
        if self.price_provider == "ccxt" and not self.ccxt_exchange:
            raise ValueError("CCXT requires ccxt_exchange")
        if self.price_provider != "ccxt" and self.ccxt_exchange is not None:
            raise ValueError("ccxt_exchange is accepted only for CCXT")
        if self.price_provider != "bloomberg" and self.bloomberg_host != "localhost":
            raise ValueError("bloomberg_host is accepted only for the bloomberg provider")
        if self.price_provider != "bloomberg" and self.bloomberg_port != 8194:
            raise ValueError("bloomberg_port is accepted only for the bloomberg provider")
        return self

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> Settings:
        """Load a bounded JSON config, then apply explicitly allowlisted environment keys."""

        values: dict[str, object] = {}
        config_name = environment.get("TRADE_RESEARCH_CONFIG")
        if config_name:
            path = Path(config_name)
            try:
                if path.stat().st_size > MAX_CONFIG_BYTES:
                    raise ProviderConfigurationError("configuration exceeds the byte limit")
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ProviderConfigurationError("configuration file is not valid JSON") from error
            if not isinstance(candidate, dict):
                raise ProviderConfigurationError("configuration must be a JSON object")
            values.update(candidate)

        names = {
            "TRADE_RESEARCH_PRICE_PROVIDER": "price_provider",
            "TRADE_RESEARCH_PRICE_PATH": "price_path",
            "TRADE_RESEARCH_FUNDAMENTAL_PROVIDER": "fundamental_provider",
            "TRADE_RESEARCH_FUNDAMENTAL_PATH": "fundamental_path",
            "TRADE_RESEARCH_CCXT_EXCHANGE": "ccxt_exchange",
            "TRADE_RESEARCH_BLOOMBERG_HOST": "bloomberg_host",
            "TRADE_RESEARCH_BLOOMBERG_PORT": "bloomberg_port",
            "TRADE_RESEARCH_DATA_ROOT": "data_root",
            "TRADE_RESEARCH_API_TOKEN": "api_token",
            "DISCORD_WEBHOOK_URL": "discord_webhook_url",
            "TRADE_RESEARCH_SEC_USER_AGENT": "sec_user_agent",
        }
        for environment_name, field_name in names.items():
            value = environment.get(environment_name)
            if value:
                values[field_name] = value
        if "price_provider" not in values and environment.get("TRADE_RESEARCH_PROVIDER"):
            values["price_provider"] = environment["TRADE_RESEARCH_PROVIDER"]
        try:
            return cls.model_validate(values)
        except ValidationError as error:
            raise ProviderConfigurationError("unknown setting or invalid configuration") from error


def _validate_local_path(provider: str, path: Path) -> None:
    if not path.is_file():
        raise ValueError("local provider path must name an existing file")
    expected_suffix = {"local_csv": ".csv", "local_parquet": ".parquet"}.get(provider)
    if expected_suffix is not None and path.suffix.lower() != expected_suffix:
        raise ValueError("local provider path has the wrong file type")


def _validate_path_containment(data_root: Path | None, path: Path) -> None:
    if data_root is None:
        return
    if not path.resolve().is_relative_to(data_root.resolve()):
        raise ValueError("local provider path must be within the configured data root")
