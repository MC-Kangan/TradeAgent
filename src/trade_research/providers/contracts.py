"""Typed, replaceable boundaries for research data sources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from trade_research.domain import Evidence, InstrumentId, Observation, Position


class ProviderConfigurationError(RuntimeError):
    """Raised when an optional provider cannot be used safely."""


class OptionalProviderDependencyError(ProviderConfigurationError):
    """Raised when an optional provider's dependency has not been installed."""


@dataclass(frozen=True)
class PricePoint:
    """One close price, including the source material needed to reproduce it."""

    observed_at: datetime
    close: float
    source: str
    provenance: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class PriceProvider(Protocol):
    """A bounded historical-price capability; callers cannot supply SQL or URLs."""

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]: ...


@runtime_checkable
class FundamentalProvider(Protocol):
    """A bounded capability for company or fund accounting observations."""

    def fundamentals(self, instrument: InstrumentId) -> tuple[Observation, ...]: ...


@runtime_checkable
class FilingProvider(Protocol):
    """A bounded capability for filing evidence."""

    def filings(self, instrument: InstrumentId) -> tuple[Evidence, ...]: ...


@runtime_checkable
class PortfolioProvider(Protocol):
    """An in-memory-only capability for user-supplied portfolio context."""

    def positions(self) -> tuple[Position, ...]: ...
