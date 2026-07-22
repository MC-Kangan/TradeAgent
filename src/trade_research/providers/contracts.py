"""Typed, replaceable boundaries for research data sources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final, NotRequired, Protocol, TypedDict, runtime_checkable

from pydantic import JsonValue

from trade_research.domain import Evidence, InstrumentId, Observation, Position
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


@runtime_checkable
class PriceProvider(Protocol):
    """A bounded historical OHLCV capability.

    Providers may return samples in any order. Consumers must sort and validate
    timestamps, duplicates, finite values, and OHLCV relationships.
    """

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]: ...


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
