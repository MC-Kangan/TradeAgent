"""Immutable, serializable values shared by every research interface."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
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


class DomainModel(BaseModel):
    """Strict domain base class with value-like semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentId(DomainModel):
    """A research instrument and the market on which it is analyzed."""

    symbol: NonEmptyText
    market: NonEmptyText

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
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
    analysts: tuple[NonEmptyText, ...] = ("fundamental", "technical")
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    positions: tuple[Position, ...] = ()

    @field_validator("analysts")
    @classmethod
    def require_analysts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one analyst must be selected")
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

    analyst: NonEmptyText
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
