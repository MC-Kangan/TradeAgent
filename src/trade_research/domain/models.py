"""Immutable, serializable values shared by every research interface."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from ipaddress import ip_address
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from trade_research.domain.provenance import (
    MetricKind,
    ProviderKind,
    normalize_metric_kind,
    normalize_provider_kind,
    sanitize_provenance,
)

SUPPORTED_MARKETS = frozenset(
    {
        "AMEX",
        "AIM",
        "BME",
        "BORSA_ITALIANA",
        "CRYPTO",
        "EU",
        "ETF",
        "EURONEXT",
        "LSE",
        "NASDAQ",
        "NYSE",
        "OTC",
        "SIX",
        "UK",
        "US",
        "XETRA",
    }
)
ASIAN_MARKETS = frozenset(
    {"ASX", "BSE", "HKEX", "JPX", "KRX", "NSE", "SET", "SGX", "SSE", "SZSE", "TSE", "TWSE"}
)

NonEmptyText = Annotated[str, Field(min_length=1)]
MAX_ANALYSTS = 16
ANALYST_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
AnalystName = Annotated[str, Field(min_length=1, max_length=64, pattern=ANALYST_PATTERN)]
SYMBOL_PATTERN = r"[A-Za-z0-9^][A-Za-z0-9._:/^-]{0,31}"
MarketSymbol = Annotated[str, Field(min_length=1, max_length=32, pattern=SYMBOL_PATTERN)]
_SYMBOL_PATTERN = re.compile(SYMBOL_PATTERN, re.IGNORECASE)
_SENSITIVE_SYMBOL_FRAGMENTS = (
    "ACCOUNT",
    "ACCT",
    "APIKEY",
    "BEARER",
    "CLIENTIP",
    "EXPOSURE",
    "HOLDING",
    "IPADDRESS",
    "PASSWORD",
    "PORTFOLIO",
    "POSITION",
    "SECRET",
    "TOKEN",
)


class DomainModel(BaseModel):
    """Strict domain base class with value-like semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentId(DomainModel):
    """A research instrument and the market on which it is analyzed."""

    symbol: MarketSymbol
    market: NonEmptyText

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        if value != value.strip() or any(character.isspace() for character in value):
            raise ValueError("symbol must not contain whitespace")
        normalized = value.upper()
        if not _SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("symbol has an invalid market-symbol format")
        try:
            ip_address(normalized)
        except ValueError:
            pass
        else:
            raise ValueError("symbol must not be an IP address")
        collapsed = re.sub(r"[^A-Z0-9]", "", normalized)
        if any(fragment in collapsed for fragment in _SENSITIVE_SYMBOL_FRAGMENTS):
            raise ValueError("symbol contains a reserved sensitive field")
        return normalized

    @field_validator("market")
    @classmethod
    def validate_market(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized in ASIAN_MARKETS:
            raise ValueError(f"market '{normalized}' is not supported")
        if normalized not in SUPPORTED_MARKETS:
            raise ValueError(f"market '{normalized}' is not supported")
        return normalized


class Position(DomainModel):
    """A typed position supplied only for in-memory research context."""

    instrument: InstrumentId
    quantity: float
    average_cost: float | None = Field(default=None, ge=0)


class AnalysisRequest(DomainModel):
    """One request for selected analysts to research an instrument."""

    request_id: UUID = Field(default_factory=uuid4)
    instrument: InstrumentId
    analysts: tuple[AnalystName, ...] = Field(
        default=("fundamental", "technical"), min_length=1, max_length=MAX_ANALYSTS
    )
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    positions: tuple[Position, ...] = ()

    @field_validator("analysts")
    @classmethod
    def require_analysts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one analyst must be selected")
        if len(value) > MAX_ANALYSTS:
            raise ValueError(f"at most {MAX_ANALYSTS} analysts may be selected")
        if any(name != name.strip() for name in value):
            raise ValueError("analyst names must not contain surrounding whitespace")
        if len(set(value)) != len(value):
            raise ValueError("analyst names must be unique")
        return value


class Observation(DomainModel):
    """One provenance-bearing fact used as research input."""

    instrument: InstrumentId
    metric: MetricKind
    value: JsonValue
    source: ProviderKind
    observed_at: datetime
    provenance: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("metric", mode="before")
    @classmethod
    def validate_metric(cls, value: object) -> MetricKind:
        return normalize_metric_kind(value)

    @field_validator("source", mode="before")
    @classmethod
    def validate_source(cls, value: object) -> ProviderKind:
        return normalize_provider_kind(value)

    @field_validator("provenance", mode="before")
    @classmethod
    def validate_provenance(cls, value: object) -> dict[str, JsonValue]:
        if not isinstance(value, Mapping):
            raise ValueError("provenance must be structured metadata")
        return sanitize_provenance(
            {str(key): candidate for key, candidate in value.items() if isinstance(key, str)}
        )


class Evidence(DomainModel):
    """Source material retained to explain an analyst conclusion."""

    source: NonEmptyText
    content: NonEmptyText
    collected_at: datetime


class AnalystResult(DomainModel):
    """The output from one independently selected analyst."""

    analyst: AnalystName
    instrument: InstrumentId
    summary: NonEmptyText
    observations: tuple[Observation, ...] = ()
    evidence: tuple[Evidence, ...] = ()


class ResearchReport(DomainModel):
    """A typed report schema common to native and containerized deployments."""

    request_id: UUID
    instrument: InstrumentId
    results: tuple[AnalystResult, ...]
    generated_at: datetime
