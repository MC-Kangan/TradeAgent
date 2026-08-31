"""Typed, replaceable boundaries for research data sources."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, NotRequired, Protocol, TypedDict, runtime_checkable

from pydantic import JsonValue

from trade_research.domain import (
    Evidence,
    InstrumentId,
    Observation,
    OutcomeSeriesSpec,
    Position,
)
from trade_research.domain.provenance import (
    PeriodRole,
    PeriodType,
    ProviderKind,
    VendorField,
    normalize_provider_kind,
    sanitize_provenance,
)


class ProviderConfigurationError(RuntimeError):
    """Raised when an optional provider cannot be used safely."""


class ProviderContractError(ProviderConfigurationError):
    """Raised when a provider violates a bounded typed return contract."""


MAX_HTTP_BYTES: Final = 4 * 1024 * 1024
MAX_LOCAL_BYTES: Final = 16 * 1024 * 1024
MAX_PRICE_POINTS: Final = 4096
MAX_OUTCOME_POINTS: Final = 8192
MAX_FUNDAMENTAL_ROWS: Final = 1024
MAX_FILING_ROWS: Final = 64


class OptionalProviderDependencyError(ProviderConfigurationError):
    """Raised when an optional provider's dependency has not been installed."""


class FundamentalStatementMetadata(TypedDict):
    """Required provenance for one statement-period observation."""

    provider_kind: ProviderKind
    snapshot_ref: str
    period_role: PeriodRole
    period_end: str
    period_type: PeriodType
    period_ref: str
    currency: str
    prior_period_ref: NotRequired[str]
    vendor_field: NotRequired[VendorField]
    reference: NotRequired[str]


class FundamentalValuationMetadata(TypedDict):
    """Required provenance for a point-in-time market valuation observation."""

    provider_kind: ProviderKind
    snapshot_ref: str
    valuation_as_of: str
    currency: str
    vendor_field: NotRequired[VendorField]
    reference: NotRequired[str]


@dataclass(frozen=True)
class PricePoint:
    """One timestamped OHLCV sample with immutable provider provenance.

    ``open``, ``high``, ``low``, and ``volume`` remain optional so close-only
    provider implementations continue to satisfy the public protocol. Skills
    label results partial when those fields are required by an indicator.
    """

    observed_at: datetime
    close: float
    source: ProviderKind | str
    provenance: Mapping[str, JsonValue] = field(default_factory=dict)
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    instrument: InstrumentId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", normalize_provider_kind(self.source))
        object.__setattr__(
            self,
            "provenance",
            MappingProxyType(sanitize_provenance(self.provenance)),
        )


@dataclass(frozen=True, slots=True)
class OutcomePoint:
    """One provider-independent scalar observation used for outcome evaluation."""

    observed_at: datetime
    value: float
    entry_value: float | None = None
    high: float | None = None
    low: float | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("outcome timestamps must include a timezone")
        values = tuple(
            value
            for value in (self.value, self.entry_value, self.high, self.low)
            if value is not None
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("outcome values must be finite")
        upper = self.value if self.high is None else self.high
        lower = self.value if self.low is None else self.low
        if lower > self.value or upper < self.value or lower > upper:
            raise ValueError("outcome high/low values must contain the observed value")

    @property
    def upper(self) -> float:
        return self.value if self.high is None else self.high

    @property
    def execution_value(self) -> float:
        """Execution-aligned value, falling back to the observed scalar series."""

        return self.value if self.entry_value is None else self.entry_value

    @property
    def lower(self) -> float:
        return self.value if self.low is None else self.low


@dataclass(frozen=True)
class OutcomeSeries:
    """One normalized bounded series with source and barrier semantics."""

    instrument: InstrumentId
    spec: OutcomeSeriesSpec
    points: tuple[OutcomePoint, ...]
    source: ProviderKind | str
    barrier_basis: Literal["observed_value", "high_low"]
    provenance: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", normalize_provider_kind(self.source))
        object.__setattr__(
            self,
            "provenance",
            MappingProxyType(sanitize_provenance(self.provenance)),
        )


@runtime_checkable
class PriceProvider(Protocol):
    """A bounded historical OHLCV capability.

    Providers may return samples in any order. Consumers must sort and validate
    timestamps, duplicates, finite values, and OHLCV relationships.
    """

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]: ...


@runtime_checkable
class OutcomeSeriesProvider(Protocol):
    """A bounded source of normalized price, volatility, or generic outcomes."""

    def outcome_history(
        self, instrument: InstrumentId, spec: OutcomeSeriesSpec
    ) -> OutcomeSeries: ...


@runtime_checkable
class FundamentalProvider(Protocol):
    """A bounded capability for company or fund accounting observations.

    Statement observations carry ``FundamentalStatementMetadata``. Market-cap
    and enterprise-value observations carry ``FundamentalValuationMetadata``.
    A valuation may be combined with a statement value only when currency and
    snapshot identity match and its as-of timestamp falls between the statement
    period end and the valuation observation's collection timestamp.
    """

    def fundamentals(self, instrument: InstrumentId) -> tuple[Observation, ...]: ...


@runtime_checkable
class FilingProvider(Protocol):
    """A bounded capability for filing evidence."""

    def filings(self, instrument: InstrumentId) -> tuple[Evidence, ...]: ...


@runtime_checkable
class PortfolioProvider(Protocol):
    """An in-memory-only capability for user-supplied portfolio context."""

    def positions(self) -> tuple[Position, ...]: ...
