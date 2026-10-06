"""Immutable, serializable values shared by every research interface."""

from __future__ import annotations

import calendar
import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from ipaddress import ip_address
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    field_validator,
    model_validator,
)

from trade_research.domain.provenance import (
    DerivedAlgorithm,
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
        "INDEX",
        "LSE",
        "NASDAQ",
        "NYSE",
        "OTC",
        "PORTFOLIO",
        "SIX",
        "SSE",
        "SZSE",
        "BJSE",
        "UK",
        "US",
        "XETRA",
    }
)
ASIAN_MARKETS = frozenset({"ASX", "BSE", "HKEX", "JPX", "KRX", "NSE", "SET", "SGX", "TSE", "TWSE"})

NonEmptyText = Annotated[str, Field(min_length=1)]
MAX_ANALYSTS = 16
ANALYST_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
AnalystName = Annotated[str, Field(min_length=1, max_length=64, pattern=ANALYST_PATTERN)]
SYMBOL_PATTERN = r"[A-Za-z0-9^][A-Za-z0-9._:/^-]{0,31}"
MarketSymbol = Annotated[str, Field(min_length=1, max_length=32, pattern=SYMBOL_PATTERN)]
_SYMBOL_PATTERN = re.compile(SYMBOL_PATTERN, re.IGNORECASE)
_AWS_ACCESS_KEY_ID_PATTERN = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")
_CREDENTIAL_PATTERN = re.compile(
    r"(?:sk|pk|rk)[-_](?:live|test|prod)[-_][A-Za-z0-9_-]{8,}"
    r"|token[-_](?:live|test|prod)[-_][A-Za-z0-9_-]{8,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
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
        if _AWS_ACCESS_KEY_ID_PATTERN.fullmatch(normalized) or _CREDENTIAL_PATTERN.fullmatch(value):
            raise ValueError("symbol resembles a credential")
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

    @model_validator(mode="after")
    def validate_market_symbol_grammar(self) -> Self:
        # Security: per-market grammar prevents URL injection by restricting characters
        # allowed in symbols before they reach provider URL construction. Non-CRYPTO
        # markets forbid '/' and ':' which are meaningful in URL paths. Provider URL
        # builders additionally use quote(symbol, safe='') as defense-in-depth.
        if self.market == "PORTFOLIO":
            pattern = r"BASKET"
        elif self.market == "CRYPTO":
            pattern = r"(?:[A-Z0-9]{2,15}(?:/|-)[A-Z0-9]{2,15}|[A-Z0-9]{2,20})"
        elif self.market == "INDEX":
            pattern = r"\^[A-Z0-9]{1,15}|[A-Z0-9][A-Z0-9.^=-]{0,15}"
        else:
            pattern = r"[A-Z0-9^][A-Z0-9.^=-]{0,15}"
        if re.fullmatch(pattern, self.symbol) is None:
            raise ValueError("symbol does not match the selected market grammar")
        return self


class Position(DomainModel):
    """A typed position supplied only for in-memory research context."""

    instrument: InstrumentId
    quantity: float
    average_cost: float | None = Field(default=None, ge=0)


class InlinePriceBar(DomainModel):
    """One bounded daily OHLCV point supplied for a single research run."""

    observed_at: datetime
    close: float = Field(gt=0)
    open: float | None = Field(default=None, gt=0)
    high: float | None = Field(default=None, gt=0)
    low: float | None = Field(default=None, gt=0)
    volume: float | None = Field(default=None, ge=0)


class InlinePriceSeries(DomainModel):
    """A caller-owned price series used without remote provider fallback."""

    instrument: InstrumentId
    source: Literal["yahoo", "tencent", "mootdx", "coinbase"]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    price_adjustment: Literal["raw", "split_adjusted", "split_dividend_adjusted"]
    daily_boundary: Literal["utc", "exchange_local"]
    source_bar_count: int | None = Field(default=None, ge=1, le=4096)
    bars: tuple[InlinePriceBar, ...] = Field(min_length=1, max_length=4096)


class OutcomeSeriesSpec(DomainModel):
    """Typed identity for the scalar series used to judge signal outcomes."""

    name: AnalystName
    kind: Literal["price", "implied_volatility", "generic"]
    unit: Literal["price", "decimal_volatility", "generic"]
    strike_convention: Literal["not_applicable", "floating_delta", "fixed_strike"] = (
        "not_applicable"
    )
    call_delta: float | None = Field(default=None, gt=0, lt=1)
    tenor: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,2}(?:d|w|m|y)$")
    strike: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_series_identity(self) -> Self:
        option_values = (self.call_delta, self.tenor, self.strike)
        if self.kind == "price":
            if self.unit != "price" or self.strike_convention != "not_applicable":
                raise ValueError("price outcome series must use price units")
            if any(value is not None for value in option_values):
                raise ValueError("price outcome series cannot contain option coordinates")
        elif self.kind == "generic":
            if self.unit != "generic" or self.strike_convention != "not_applicable":
                raise ValueError("generic outcome series must use generic units")
            if any(value is not None for value in option_values):
                raise ValueError("generic outcome series cannot contain option coordinates")
        else:
            if self.unit != "decimal_volatility" or self.tenor is None:
                raise ValueError(
                    "implied-volatility outcome series require decimal units and tenor"
                )
            if self.strike_convention == "floating_delta":
                if self.call_delta is None or self.strike is not None:
                    raise ValueError(
                        "floating-delta volatility requires call_delta and no fixed strike"
                    )
            elif self.strike_convention == "fixed_strike":
                if self.strike is None or self.call_delta is not None:
                    raise ValueError("fixed-strike volatility requires strike and no call_delta")
            else:
                raise ValueError("implied volatility requires a strike convention")
        return self


class InlineOutcomePoint(DomainModel):
    """One caller-supplied observation in a normalized scalar outcome series."""

    observed_at: datetime
    value: float
    high: float | None = None
    low: float | None = None

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        values = tuple(value for value in (self.value, self.high, self.low) if value is not None)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("outcome values must be finite")
        upper = self.value if self.high is None else self.high
        lower = self.value if self.low is None else self.low
        if lower > self.value or upper < self.value or lower > upper:
            raise ValueError("outcome high/low values must contain the observed value")
        return self


class InlineOutcomeSeries(DomainModel):
    """A bounded normalized outcome series supplied for one immediate research run."""

    instrument: InstrumentId
    spec: OutcomeSeriesSpec
    source: ProviderKind
    barrier_basis: Literal["observed_value", "high_low"]
    points: tuple[InlineOutcomePoint, ...] = Field(min_length=1, max_length=8192)

    @model_validator(mode="after")
    def validate_points(self) -> Self:
        timestamps = [point.observed_at for point in self.points]
        if any(timestamp.tzinfo is None for timestamp in timestamps):
            raise ValueError("outcome timestamps must include a timezone")
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            raise ValueError("outcome points must have ordered unique timestamps")
        complete = [point.high is not None and point.low is not None for point in self.points]
        partial = [(point.high is None) != (point.low is None) for point in self.points]
        if any(partial):
            raise ValueError("outcome high and low must be supplied together")
        if self.barrier_basis == "high_low" and not all(complete):
            raise ValueError("high_low barrier basis requires high and low for every point")
        if self.barrier_basis == "observed_value" and any(complete):
            raise ValueError("observed_value barrier basis cannot include high or low")
        return self


MAX_FACTOR_COUNT = 64
MAX_FACTOR_INPUTS = 2 * MAX_FACTOR_COUNT + 1
FactorFrequency = Literal["daily", "monthly"]
FactorRegion = Literal["US", "Europe"]
FactorId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class FactorDefinition(DomainModel):
    id: FactorId
    label: str = Field(min_length=1, max_length=96)
    kind: Literal["asset_return", "excess_return", "spread", "change"]
    unit: str = Field(
        default="decimal_return", min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_/%.-]+$"
    )

    @model_validator(mode="after")
    def validate_unit(self) -> Self:
        if self.kind != "change" and self.unit != "decimal_return":
            raise ValueError("return factors must use decimal_return units")
        return self


class FactorSpec(DomainModel):
    """A selected normalized research column or one/two funded return legs."""

    id: FactorId
    label: str = Field(min_length=1, max_length=96)
    kind: Literal["asset_return", "spread", "research"]
    instrument: InstrumentId | None = None
    short_instrument: InstrumentId | None = None
    research_key: FactorId | None = None
    calendar: str | None = Field(default=None, max_length=32)
    short_calendar: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_legs(self) -> Self:
        if self.id == "intercept":
            raise ValueError("intercept is a reserved term")
        if self.kind == "research":
            if not self.research_key or any(
                (self.instrument, self.short_instrument, self.calendar, self.short_calendar)
            ):
                raise ValueError("research factors require only a research key")
        elif self.instrument is None or self.research_key is not None:
            raise ValueError("asset factors require an instrument, not a research key")
        elif self.kind == "spread":
            if self.short_instrument is None or self.short_instrument == self.instrument:
                raise ValueError("spreads require distinct long and short instruments")
        elif self.short_instrument is not None or self.short_calendar is not None:
            raise ValueError("a single asset cannot have a short leg")
        return self


class FactorReturnPoint(DomainModel):
    """Decimal simple total return over an explicit pair of session dates."""

    start_date: date
    end_date: date
    value: FiniteFloat = Field(ge=-1)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.start_date >= self.end_date:
            raise ValueError("return intervals must increase")
        return self


class FactorReturnSeries(DomainModel):
    """Normalized historical returns, transient inputs rather than report data."""

    instrument: InstrumentId
    frequency: FactorFrequency = "daily"
    calendar: str | None = Field(default=None, max_length=32)
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    return_basis: Literal["gross_total_return", "net_total_return", "adjusted_close_return"]
    source: ProviderKind
    vendor_field: Literal["ADJ_CLOSE", "TOTAL_RETURN", "PX_LAST", "TOTAL_RETURN_INDEX_GROSS_DVDS"]
    retrieved_at: datetime
    points: tuple[FactorReturnPoint, ...] = Field(max_length=4096)

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieval time must include a timezone")
        for index, point in enumerate(self.points):
            if point.end_date > self.retrieved_at.date():
                raise ValueError("return interval is future-dated")
            if index and point.start_date < self.points[index - 1].end_date:
                raise ValueError("return intervals must be ordered and nonoverlapping")
        return self


class ResearchFactorPoint(DomainModel):
    date: date
    values: dict[FactorId, FiniteFloat] = Field(min_length=1, max_length=MAX_FACTOR_COUNT)
    risk_free: FiniteFloat | None = Field(default=None, ge=-1)


class ResearchFactorPanel(DomainModel):
    """Provider-independent native-frequency factor observations."""

    region: FactorRegion
    frequency: FactorFrequency
    currency: Currency
    calendar: str = Field(min_length=1, max_length=32)
    definitions: tuple[FactorDefinition, ...] = Field(min_length=1, max_length=MAX_FACTOR_COUNT)
    source: ProviderKind
    retrieved_at: datetime
    reference: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    points: tuple[ResearchFactorPoint, ...] = Field(max_length=4096)

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        days = [p.date for p in self.points]
        if self.frequency == "monthly" and any(
            d.day != calendar.monthrange(d.year, d.month)[1] for d in days
        ):
            raise ValueError("monthly research dates require calendar month-end labels")
        ids = [d.id for d in self.definitions]
        if len(ids) != len(set(ids)) or "intercept" in ids:
            raise ValueError("research column IDs must be unique and not intercept")
        if any(set(p.values) != set(ids) for p in self.points):
            raise ValueError("research values must match declared definitions")
        if self.retrieved_at.tzinfo is None or days != sorted(set(days)):
            raise ValueError("factor dates must be unique/ordered and retrieval timezone-aware")
        if days and days[-1] > self.retrieved_at.date():
            raise ValueError("future factor observation")
        return self


class FxLevelPoint(DomainModel):
    date: date
    value: FiniteFloat = Field(gt=0)


class FxLevelSeries(DomainModel):
    """Quote currency per unit of base currency at dated closes; never forward filled."""

    base_currency: Currency
    quote_currency: Currency = "USD"
    source: ProviderKind
    retrieved_at: datetime
    points: tuple[FxLevelPoint, ...] = Field(max_length=4097)

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        days = [p.date for p in self.points]
        if self.retrieved_at.tzinfo is None or days != sorted(set(days)):
            raise ValueError("FX dates must be ordered/unique and retrieval timezone-aware")
        if days and days[-1] > self.retrieved_at.date():
            raise ValueError("future FX observation")
        return self


class AnalysisRequest(DomainModel):
    """One bounded instrument or portfolio research request."""

    request_id: UUID = Field(default_factory=uuid4)
    instrument: InstrumentId
    scope: Literal["instrument", "portfolio"] = "instrument"
    portfolio_instruments: tuple[InstrumentId, ...] = Field(default=(), max_length=64)
    analysts: tuple[AnalystName, ...] = Field(
        default=("fundamental", "technical"), min_length=1, max_length=MAX_ANALYSTS
    )
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    positions: tuple[Position, ...] = ()
    skill_parameters: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    price_series: tuple[InlinePriceSeries, ...] = Field(default=(), max_length=9)
    outcome_series: tuple[InlineOutcomeSeries, ...] = Field(default=(), max_length=9)
    factor_series: tuple[FactorReturnSeries, ...] = Field(default=(), max_length=MAX_FACTOR_INPUTS)
    research_factors: ResearchFactorPanel | None = None
    fx_series: tuple[FxLevelSeries, ...] = Field(default=(), max_length=MAX_FACTOR_INPUTS)

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

    @model_validator(mode="after")
    def validate_scope_and_price_series(self) -> Self:
        if self.research_factors is not None or self.fx_series:
            if self.scope != "instrument" or "factor-regression" not in self.analysts:
                raise ValueError("research factors and FX require instrument factor regression")
            if len({(x.base_currency, x.quote_currency) for x in self.fx_series}) != len(
                self.fx_series
            ):
                raise ValueError("FX currencies must be unique")
        if self.factor_series:
            factor_regression = self.scope == "instrument" and "factor-regression" in self.analysts
            cross_section = (
                self.scope == "portfolio" and "cross-sectional-signal" in self.analysts
            )
            if not (factor_regression or cross_section):
                raise ValueError(
                    "factor series require factor regression or cross-sectional signal research"
                )
            identities = tuple(item.instrument for item in self.factor_series)
            if len(set(identities)) != len(identities):
                raise ValueError("factor series instruments must be unique")
        basket = InstrumentId(symbol="BASKET", market="PORTFOLIO")
        if self.scope == "portfolio":
            if self.instrument != basket:
                raise ValueError("portfolio requests must use the PORTFOLIO:BASKET identity")
            if not 2 <= len(self.portfolio_instruments) <= 64:
                raise ValueError("portfolio requests require between 2 and 64 instruments")
            if len(set(self.portfolio_instruments)) != len(self.portfolio_instruments):
                raise ValueError("portfolio instruments must be unique")
            if any(item.market == "PORTFOLIO" for item in self.portfolio_instruments):
                raise ValueError("portfolio constituents must be tradable instruments")
        elif self.instrument.market == "PORTFOLIO" or self.portfolio_instruments:
            raise ValueError(
                "instrument requests cannot contain a portfolio identity or constituents"
            )
        instruments = tuple(item.instrument for item in self.price_series)
        if len(set(instruments)) != len(instruments):
            raise ValueError("price series instruments must be unique")
        if self.scope == "portfolio" and self.price_series:
            supplied = set(instruments)
            required = set(self.portfolio_instruments)
            if supplied != required:
                raise ValueError("portfolio price series must exactly match portfolio instruments")
        if self.scope == "portfolio" and self.factor_series:
            if set(item.instrument for item in self.factor_series) != set(
                self.portfolio_instruments
            ):
                raise ValueError(
                    "portfolio factor series must exactly match portfolio instruments"
                )
        outcome_keys = tuple((item.instrument, item.spec.name) for item in self.outcome_series)
        if len(set(outcome_keys)) != len(outcome_keys):
            raise ValueError("outcome series instrument/name pairs must be unique")
        if self.scope == "portfolio" and self.outcome_series:
            raise ValueError("portfolio requests cannot contain outcome series")
        if self.scope == "instrument" and any(
            item.instrument != self.instrument for item in self.outcome_series
        ):
            raise ValueError("outcome series must match the requested instrument")
        return self


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


class ReportStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class FailureCategory(StrEnum):
    NONE = "none"
    PROVIDER_CONFIGURATION = "provider_configuration"
    PROVIDER_CONTRACT = "provider_contract"
    ANALYST_ERROR = "analyst_error"
    INSUFFICIENT_DATA = "insufficient_data"


class LimitationKind(StrEnum):
    MISSING_INPUTS = "missing_inputs"
    INCOMPATIBLE_INPUTS = "incompatible_inputs"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INCOMPLETE_OHLCV = "incomplete_ohlcv"
    INVALID_ROWS_DISCARDED = "invalid_rows_discarded"
    PROVIDER_FAILURE = "provider_failure"
    ANALYST_FAILURE = "analyst_failure"
    EVIDENCE_OMITTED = "evidence_omitted"
    BOUNDED_INPUT = "bounded_input"
    UNEXECUTED_SIGNALS = "unexecuted_signals"


class InferenceKind(StrEnum):
    FACTOR_ANALYSIS = "factor_analysis"
    NOT_AVAILABLE = "not_available"


class SignalKind(StrEnum):
    NOT_ASSESSED = "not_assessed"
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


OpaqueReference = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
MethodWindow = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]


class Citation(DomainModel):
    provider: ProviderKind
    reference: OpaqueReference
    collected_at: datetime


class AnalysisMethod(DomainModel):
    algorithm: DerivedAlgorithm
    window: MethodWindow


class ReportPriceBar(DomainModel):
    """One bounded OHLCV point intended for deterministic report charts."""

    observed_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class ReportBenchmark(DomainModel):
    """A benchmark requested by a skill and its provider-facing instrument."""

    label: MarketSymbol
    instrument: InstrumentId
    available: bool


class ReportScoreComponent(DomainModel):
    """A normalized worth-buy score component and its weighted contribution."""

    key: Literal["momentum", "relative_strength", "trend_efficiency"]
    score: float | None = Field(default=None, ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    weighted_score: float = Field(ge=0, le=100)


class ReportCheck(DomainModel):
    """A deterministic risk or confirmation check for presentation."""

    key: Annotated[str, Field(min_length=1, max_length=48, pattern=r"^[a-z0-9_]+$")]
    status: Literal["pass", "warning", "fail", "unavailable"]
    value: float | None = None


class ReportReferenceLevels(DomainModel):
    """Model-derived reference levels; these are not order instructions."""

    entry: float | None = Field(default=None, ge=0)
    stop: float | None = Field(default=None, ge=0)
    target: float | None = Field(default=None, ge=0)


class WorthBuyPresentation(DomainModel):
    """Bounded data used by clients to render the worth-buy report template."""

    template: Literal["worth-buy-stocks-v1"] = "worth-buy-stocks-v1"
    verdict: Literal["buy", "watch", "avoid", "reduce_risk", "cannot_score"]
    entry_class: Literal[
        "trend_broken",
        "overextended",
        "pullback_no_trigger",
        "trend_continuation",
        "pullback_reversal",
        "recovery_reversal",
        "unknown",
    ]
    benchmarks: tuple[ReportBenchmark, ...] = Field(default=(), max_length=8)
    score_components: tuple[ReportScoreComponent, ...] = Field(default=(), max_length=3)
    risk_checks: tuple[ReportCheck, ...] = Field(default=(), max_length=8)
    confirmation_checks: tuple[ReportCheck, ...] = Field(default=(), max_length=10)
    reference_levels: ReportReferenceLevels = Field(default_factory=ReportReferenceLevels)
    price_bars: tuple[ReportPriceBar, ...] = Field(default=(), max_length=60)


class ReportMarkovRegimePoint(DomainModel):
    """One bounded point used to render Markov regime context."""

    observed_at: datetime
    close: float = Field(gt=0)
    rolling_return: float
    regime: Literal["Bear", "Sideways", "Bull"]


class MarkovPresentation(DomainModel):
    """Bounded data used by clients to render the Markov framework."""

    template: Literal["markov-method-v1"] = "markov-method-v1"
    window: int = Field(ge=2, le=252)
    bull_threshold: float = Field(gt=0, le=1)
    bear_threshold: float = Field(ge=-1, lt=0)
    current_regime: Literal["Bear", "Sideways", "Bull"]
    transition_matrix: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    stationary_distribution: tuple[float, float, float]
    regime_points: tuple[ReportMarkovRegimePoint, ...] = Field(default=(), max_length=520)


class PortfolioAssetStat(DomainModel):
    """One asset's bounded statistics in a portfolio presentation."""

    instrument: InstrumentId
    annualized_volatility: float = Field(ge=0)
    weight: float | None = Field(default=None, ge=0, le=1)
    risk_contribution: float | None = None


class CorrelationPresentation(DomainModel):
    """Aligned multi-asset return correlation data for client charts."""

    template: Literal["correlation-analysis-v1"] = "correlation-analysis-v1"
    assets: tuple[PortfolioAssetStat, ...] = Field(min_length=2, max_length=9)
    correlation_matrix: tuple[tuple[float, ...], ...] = Field(min_length=2, max_length=9)
    aligned_return_count: int = Field(ge=2, le=519)
    lookback: int = Field(ge=20, le=252)

    @model_validator(mode="after")
    def validate_square_matrix(self) -> Self:
        size = len(self.assets)
        if len(self.correlation_matrix) != size or any(
            len(row) != size for row in self.correlation_matrix
        ):
            raise ValueError("correlation matrix dimensions must match assets")
        return self


class AssetAllocationPresentation(DomainModel):
    """Long-only mathematical allocation scenario derived from price history."""

    template: Literal["asset-allocation-v1"] = "asset-allocation-v1"
    method: Literal["equal_weight", "inverse_volatility", "risk_parity", "max_diversification"]
    assets: tuple[PortfolioAssetStat, ...] = Field(min_length=2, max_length=9)
    correlation_matrix: tuple[tuple[float, ...], ...] = Field(min_length=2, max_length=9)
    aligned_return_count: int = Field(ge=2, le=519)
    lookback: int = Field(ge=20, le=252)
    portfolio_volatility: float = Field(ge=0)
    diversification_ratio: float = Field(ge=0)
    effective_asset_count: float = Field(ge=1, le=9)

    @model_validator(mode="after")
    def validate_allocation(self) -> Self:
        size = len(self.assets)
        if len(self.correlation_matrix) != size or any(
            len(row) != size for row in self.correlation_matrix
        ):
            raise ValueError("correlation matrix dimensions must match assets")
        weights = [item.weight for item in self.assets]
        if any(value is None for value in weights) or not math.isclose(
            sum(value for value in weights if value is not None), 1.0, abs_tol=1e-8
        ):
            raise ValueError("allocation weights must be fully invested")
        return self


class BacktestCurvePoint(DomainModel):
    """One bounded equity-curve point from a completed simulation."""

    observed_at: datetime
    equity: float = Field(ge=0)
    drawdown: float = Field(ge=0, le=1)


class BacktestIndicatorPoint(DomainModel):
    """One chart-ready value produced by the selected backtest strategy."""

    observed_at: datetime
    value: float


class BacktestIndicatorSeries(DomainModel):
    """A bounded renderer-neutral strategy indicator series."""

    key: Annotated[str, Field(min_length=1, max_length=48, pattern=r"^[a-z0-9_]+$")]
    label: Annotated[str, Field(min_length=1, max_length=64)]
    panel: Literal["price", "oscillator", "regime"]
    points: tuple[BacktestIndicatorPoint, ...] = Field(default=(), max_length=520)


class BacktestTrade(DomainModel):
    """One closed long trade, expressed as research output rather than an order."""

    entry_at: datetime
    exit_at: datetime
    size: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    pnl: float
    commission: float = Field(ge=0)
    return_ratio: float
    duration_bars: int = Field(ge=0)
    exit_reason: Literal["strategy_reduce", "strategy_exit", "stop_loss", "take_profit", "unknown"]


class BacktestOpenPosition(DomainModel):
    """One long position still open and marked to the final test close."""

    entry_at: datetime
    size: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    unrealized_pnl: float
    return_ratio: float
    duration_bars: int = Field(ge=0)


class BacktestStrategyParameter(DomainModel):
    """One closed strategy setting retained for reproducibility."""

    key: Literal[
        "fast_window",
        "slow_window",
        "signal_window",
        "rsi_window",
        "entry_threshold",
        "exit_threshold",
        "regime_window",
        "bull_threshold",
        "bear_threshold",
        "min_train",
        "event_count",
    ]
    value: int | float


class BacktestAssumptions(DomainModel):
    """Bounded simulation settings required to reproduce a backtest."""

    execution: Literal["signal_close_next_open"] = "signal_close_next_open"
    start_date: date | None = None
    minimum_holding_bars: int = Field(default=1, ge=1, le=520)
    position_budget: float = Field(gt=0)
    commission: float = Field(ge=0)
    spread: float = Field(ge=0)
    tranche_fraction: float = Field(gt=0, le=1)
    deployment_cap_fraction: float = Field(gt=0, le=1)
    minimum_addition_bars: int = Field(default=1, ge=1, le=520)
    signal_horizon_bars: int = Field(default=21, ge=1, le=520)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)
    take_profit_pct: float | None = Field(default=None, gt=0)
    strategy_parameters: tuple[BacktestStrategyParameter, ...] = Field(max_length=11)
    configuration_reference: OpaqueReference


class BacktestSignalQuality(DomainModel):
    """Entry-signal outcomes before position sizing and execution filters."""

    status: Literal["complete", "partial", "insufficient_history", "no_signals"]
    entry_lag_bars: Literal[1] = 1
    horizon_bars: int = Field(ge=1, le=520)
    source_add_signal_count: int = Field(ge=0)
    evaluated_signal_count: int = Field(ge=0)
    independent_signal_count: int = Field(ge=0)
    skipped_signal_count: int = Field(ge=0)
    win_rate: float | None = Field(default=None, ge=0, le=1)
    expected_change: float | None = None
    payoff_ratio: float | None = Field(default=None, gt=0)
    average_favorable_change: float | None = Field(default=None, ge=0)
    average_adverse_change: float | None = Field(default=None, le=0)
    holdout_start_at: datetime
    holdout_signal_count: int = Field(ge=0)
    holdout_evaluated_signal_count: int = Field(ge=0)
    holdout_independent_signal_count: int = Field(ge=0)
    holdout_win_rate: float | None = Field(default=None, ge=0, le=1)
    holdout_expected_change: float | None = None
    holdout_payoff_ratio: float | None = Field(default=None, gt=0)


class BacktestExecutionAudit(DomainModel):
    """Aggregate bridge from canonical strategy events to simulated lots."""

    add_signal_count: int = Field(ge=0)
    reduce_signal_count: int = Field(ge=0)
    exit_signal_count: int = Field(ge=0)
    executed_addition_count: int = Field(ge=0)
    unexecuted_addition_count: int = Field(ge=0)
    submitted_reduction_count: int = Field(ge=0)
    submitted_exit_count: int = Field(ge=0)
    executed_reduction_count: int = Field(ge=0)
    executed_exit_count: int = Field(ge=0)
    stop_loss_exit_count: int = Field(ge=0)
    take_profit_exit_count: int = Field(ge=0)
    delayed_signal_count: int = Field(ge=0)
    rejected_signal_count: int = Field(ge=0)
    rejected_allocation_cap_count: int = Field(ge=0)
    rejected_insufficient_cash_count: int = Field(ge=0)
    rejected_cooldown_count: int = Field(ge=0)
    rejected_exit_pending_count: int = Field(ge=0)
    ignored_no_position_count: int = Field(ge=0)
    unexecuted_addition_boundary_count: int = Field(ge=0)
    unexecuted_reduction_boundary_count: int = Field(ge=0)
    unexecuted_exit_boundary_count: int = Field(ge=0)
    total_costs: float = Field(ge=0)
    average_entry_fill_price: float | None = Field(default=None, gt=0)
    average_deployed_capital: float = Field(ge=0)
    maximum_deployed_capital: float = Field(ge=0)
    average_exposure_fraction: float = Field(ge=0)
    maximum_exposure_fraction: float = Field(ge=0)


class BacktestDataQuality(DomainModel):
    """Calculation coverage and market-data conventions for one backtest."""

    source_bar_count: int = Field(ge=1)
    valid_bar_count: int = Field(ge=1)
    discarded_bar_count: int = Field(ge=0)
    warmup_bar_count: int = Field(ge=0)
    calculation_bar_count: int = Field(ge=1)
    presented_bar_count: int = Field(ge=1, le=520)
    calculation_start_at: datetime
    calculation_end_at: datetime
    quote_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    price_adjustment: Literal["raw", "split_adjusted", "split_dividend_adjusted"] | None = None
    daily_boundary: Literal["utc", "exchange_local"] | None = None


class BacktestPositionPerformance(DomainModel):
    """Net result of applying the execution policy to the signal stream."""

    final_equity: float = Field(ge=0)
    total_return: float
    return_on_average_deployed_capital: float | None = None
    buy_hold_return: float
    exposure_adjusted_buy_hold_return: float
    max_drawdown: float = Field(ge=0, le=1)
    realized_pnl: float
    total_pnl: float
    gross_turnover_ratio: float = Field(ge=0)
    closed_lot_count: int = Field(ge=0)
    open_lot_count: int = Field(ge=0)
    open_total_size: float = Field(ge=0)
    open_average_entry_price: float | None = Field(default=None, gt=0)
    open_unrealized_pnl: float
    win_rate: float | None = Field(default=None, ge=0, le=1)
    average_winner: float | None = Field(default=None, gt=0)
    average_loser: float | None = Field(default=None, gt=0)
    payoff_ratio: float | None = Field(default=None, gt=0)
    net_expectancy: float | None = None
    profit_factor: float | None = Field(default=None, gt=0)
    sharpe_ratio: float | None = None


class BacktestPresentation(DomainModel):
    """Bounded, renderer-neutral output for a reproducible backtest."""

    template: Literal["backtesting-v2"] = "backtesting-v2"
    engine: Literal["backtesting.py"] = "backtesting.py"
    engine_version: Annotated[str, Field(min_length=1, max_length=16)]
    strategy_kind: Literal[
        "sma_crossover",
        "macd_crossover",
        "rsi_mean_reversion",
        "markov_regime",
        "external_signals",
    ]
    strategy_name: AnalystName
    signal_reference: OpaqueReference
    assumptions: BacktestAssumptions
    data_quality: BacktestDataQuality
    signal_quality: BacktestSignalQuality
    execution_audit: BacktestExecutionAudit
    position_performance: BacktestPositionPerformance
    price_bars: tuple[ReportPriceBar, ...] = Field(default=(), max_length=520)
    indicator_series: tuple[BacktestIndicatorSeries, ...] = Field(default=(), max_length=4)
    curve: tuple[BacktestCurvePoint, ...] = Field(default=(), max_length=520)
    trades: tuple[BacktestTrade, ...] = Field(default=(), max_length=200)
    open_positions: tuple[BacktestOpenPosition, ...] = Field(default=(), max_length=100)
    presentation_reduced: bool = False


class ReportSignalOutcome(DomainModel):
    """One bounded historical outcome produced from a timestamped instruction."""

    signal_at: datetime
    entry_at: datetime
    exit_at: datetime
    direction: Literal["long", "short"]
    entry_value: float
    exit_value: float
    change: float
    initial_risk: float = Field(gt=0)
    r_multiple: float
    maximum_favorable_change: float = Field(ge=0)
    maximum_adverse_change: float = Field(le=0)
    maximum_favorable_r: float = Field(ge=0)
    maximum_adverse_r: float = Field(le=0)
    duration_bars: int = Field(ge=1, le=4096)
    exit_reason: Literal["fixed_horizon", "profit_target", "stop_loss", "time_limit"]
    same_bar_ambiguous: bool = False


class SignalDirectionSummary(DomainModel):
    """Compact performance split for one signal direction."""

    direction: Literal["long", "short"]
    event_count: int = Field(ge=0)
    win_rate: float | None = Field(default=None, ge=0, le=1)
    expected_r: float | None = None


class SignalEvaluationSummary(DomainModel):
    """Performance statistics for one historical outcome definition."""

    event_count: int = Field(ge=0)
    non_overlapping_event_count: int = Field(ge=0)
    skipped_event_count: int = Field(ge=0)
    purged_event_count: int = Field(default=0, ge=0)
    win_count: int = Field(ge=0)
    loss_count: int = Field(ge=0)
    breakeven_count: int = Field(ge=0)
    win_rate: float | None = Field(default=None, ge=0, le=1)
    non_overlapping_win_rate: float | None = Field(default=None, ge=0, le=1)
    non_overlapping_win_rate_lower_95: float | None = Field(default=None, ge=0, le=1)
    average_win: float | None = Field(default=None, gt=0)
    average_loss: float | None = Field(default=None, gt=0)
    average_win_r: float | None = Field(default=None, gt=0)
    average_loss_r: float | None = Field(default=None, gt=0)
    reward_risk_ratio: float | None = Field(default=None, gt=0)
    win_payoff_product: float | None = Field(default=None, ge=0)
    break_even_win_rate: float | None = Field(default=None, ge=0, le=1)
    edge_over_break_even: float | None = Field(default=None, ge=-1, le=1)
    expected_value: float | None = None
    expected_r: float | None = None
    non_overlapping_expected_r: float | None = None
    non_overlapping_expected_r_lower_95: float | None = None
    non_overlapping_expected_r_median: float | None = None
    non_overlapping_expected_r_upper_95: float | None = None
    bootstrap_positive_fraction: float | None = Field(default=None, ge=0, le=1)
    bootstrap_block_length: int | None = Field(default=None, ge=1, le=520)
    profit_factor: float | None = Field(default=None, gt=0)
    top_five_win_contribution: float | None = Field(default=None, ge=0, le=1)
    baseline_trial_count: int = Field(default=0, ge=0, le=5000)
    baseline_expected_r: float | None = None
    baseline_expected_r_lower_95: float | None = None
    baseline_expected_r_upper_95: float | None = None
    excess_expected_r: float | None = None
    directions: tuple[SignalDirectionSummary, ...] = Field(default=(), max_length=2)
    events: tuple[ReportSignalOutcome, ...] = Field(default=(), max_length=200)
    presentation_reduced: bool = False


class SignalExperimentDefinition(DomainModel):
    """Safe identity for a reproducible signal experiment."""

    experiment_id: AnalystName
    strategy_version: Annotated[
        str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ]
    strategy_frozen_at: datetime
    evaluation_data_end: datetime
    variant_count: int = Field(default=1, ge=1, le=1_000_000)
    parameters_reference: OpaqueReference | None = None
    holdout_is_post_freeze: bool | None = None

    @field_validator("strategy_frozen_at", "evaluation_data_end")
    @classmethod
    def require_aware_experiment_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("experiment timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_experiment_window(self) -> Self:
        if self.evaluation_data_end < self.strategy_frozen_at:
            raise ValueError("evaluation_data_end must not precede strategy_frozen_at")
        return self


class SignalEvaluationPeriodPresentation(DomainModel):
    """One chronology-preserving evaluation slice."""

    name: Literal["development", "validation", "holdout"]
    start_at: datetime
    end_at: datetime
    fixed_horizon: SignalEvaluationSummary
    triple_barrier: SignalEvaluationSummary

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("evaluation period timestamps must include a timezone")
        if self.end_at <= self.start_at:
            raise ValueError("evaluation period end must follow its start")
        return self


class SignalEvaluationPresentation(DomainModel):
    """Renderer-neutral fixed-horizon and triple-barrier signal study."""

    template: Literal["signal-evaluation-v2"] = "signal-evaluation-v2"
    evaluation_purpose: Literal["outcome_expectancy"] = "outcome_expectancy"
    signal_name: AnalystName
    experiment: SignalExperimentDefinition
    target_series: OutcomeSeriesSpec
    change_kind: Literal["relative", "absolute"]
    barrier_basis: Literal["observed_value", "high_low"]
    entry_lag_bars: int = Field(ge=1, le=520)
    fixed_horizon_bars: int = Field(ge=1, le=520)
    profit_target: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    max_holding_bars: int = Field(ge=1, le=520)
    same_bar_policy: Literal["loss"] = "loss"
    signal_reference: OpaqueReference
    series_reference: OpaqueReference
    configuration_reference: OpaqueReference
    fixed_horizon: SignalEvaluationSummary
    triple_barrier: SignalEvaluationSummary
    periods: tuple[SignalEvaluationPeriodPresentation, ...] = Field(default=(), max_length=3)


class PriceActionBar(DomainModel):
    """One daily OHLC point used by the price-action report."""

    observed_at: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float | None = Field(default=None, ge=0)


class PriceActionPivot(DomainModel):
    """One confirmed, non-lookahead swing point."""

    observed_at: datetime
    price: float = Field(gt=0)
    kind: Literal["high", "low"]
    status: Literal["confirmed"] = "confirmed"
    label: Literal["HH", "LH", "HL", "LL"] | None = None


class PriceActionZone(DomainModel):
    """An ATR-scaled cluster of confirmed swing points."""

    kind: Literal["support", "resistance", "flip"]
    lower: float = Field(gt=0)
    midpoint: float = Field(gt=0)
    upper: float = Field(gt=0)
    touch_count: int = Field(ge=2, le=64)
    last_touched_at: datetime

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if not self.lower <= self.midpoint <= self.upper:
            raise ValueError("zone bounds must contain midpoint")
        return self


class PriceActionCandleEvent(DomainModel):
    """One completed daily candle-pattern event."""

    kind: Literal[
        "bullish_rejection",
        "bearish_rejection",
        "inside_bar",
        "bullish_engulfing",
        "bearish_engulfing",
    ]
    direction: Literal["bullish", "bearish", "neutral"]
    started_at: datetime
    observed_at: datetime
    price: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.started_at.tzinfo is None or self.observed_at.tzinfo is None:
            raise ValueError("candle event timestamps must include a timezone")
        if self.observed_at < self.started_at:
            raise ValueError("candle event completion must not precede its start")
        expected_direction = (
            "neutral"
            if self.kind == "inside_bar"
            else "bullish"
            if self.kind.startswith("bullish_")
            else "bearish"
        )
        if self.direction != expected_direction:
            raise ValueError("candle event direction must match its pattern kind")
        return self


class PriceActionStructurePresentation(DomainModel):
    """Bounded, renderer-neutral daily structure and price-zone output."""

    template: Literal["price-action-structure-v2"] = "price-action-structure-v2"
    timeframe: Literal["1d"] = "1d"
    structure: Literal["uptrend", "downtrend", "mixed", "unavailable"]
    atr_14: float = Field(ge=0)
    price_bars: tuple[PriceActionBar, ...] = Field(default=(), max_length=180)
    pivots: tuple[PriceActionPivot, ...] = Field(default=(), max_length=64)
    zones: tuple[PriceActionZone, ...] = Field(default=(), max_length=8)
    events: tuple[PriceActionCandleEvent, ...] = Field(default=(), max_length=10)


FactorTerm = FactorId

FactorDiagnostic = Literal[
    "currency_mismatch",
    "return_basis_mismatch",
    "insufficient_history",
    "rank_deficient",
    "constant_target",
    "high_collinearity",
    "original_high_collinearity",
    "short_history",
    "missing_intervals",
    "discontinuous_history",
    "long_intervals_excluded",
    "raw_returns_not_alpha",
    "nonsynchronous_closes",
    "unsupported_market",
    "numerical_failure",
    "hac_intervals_withheld",
    "unsupported_frequency",
    "factor_definition_mismatch",
    "calendar_unavailable",
    "research_data_lag",
    "research_data_revised",
    "fx_conversion",
    "fx_missing",
    "benchmark_self_inclusion_unchecked",
    "influential_observations",
    "rolling_windows_skipped",
    "return_convention_difference",
]


class FactorCoefficient(DomainModel):
    term: FactorTerm
    label: str
    unit: str
    estimate: FiniteFloat
    standard_error: FiniteFloat | None = Field(default=None, ge=0)
    lower_95: FiniteFloat | None = None
    upper_95: FiniteFloat | None = None
    standardized_effect: FiniteFloat | None = None


FactorInputRole = Annotated[str, Field(min_length=1, max_length=80)]


class FactorInputSummary(DomainModel):
    role: FactorInputRole
    instrument: InstrumentId
    source: ProviderKind
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    return_basis: Literal["gross_total_return", "net_total_return", "adjusted_close_return"]
    vendor_field: Literal["ADJ_CLOSE", "TOTAL_RETURN", "PX_LAST", "TOTAL_RETURN_INDEX_GROSS_DVDS"]
    reference: OpaqueReference
    retrieved_at: datetime
    interval_count: int = Field(ge=0, le=4096)


class RollingFactorFit(DomainModel):
    end_date: date
    start_date: date
    sample_count: int
    estimates: tuple[FiniteFloat, ...] = Field(max_length=MAX_FACTOR_COUNT + 1)
    r_squared: FiniteFloat


class FactorDatasetSummary(DomainModel):
    source: ProviderKind
    reference: OpaqueReference
    retrieved_at: datetime
    currency: str
    label: str
    observation_count: int


class FactorCoverage(DomainModel):
    role: str
    expected_periods: int
    available_periods: int
    invalid_or_missing_periods: int
    fx_endpoint_losses: int = 0
    alignment_losses: int = 0


class FactorModelSpec(DomainModel):
    name: str = Field(min_length=1, max_length=64)
    factor_ids: tuple[FactorId, ...] = Field(min_length=1, max_length=MAX_FACTOR_COUNT)


class FactorResidualization(DomainModel):
    factor_id: FactorId
    against: tuple[FactorId, ...] = Field(min_length=1, max_length=MAX_FACTOR_COUNT)


class FactorResidualizationSummary(FactorResidualization):
    remaining_variance_fraction: FiniteFloat = Field(ge=0, le=1)


class FactorModelCoefficient(DomainModel):
    term: FactorId
    label: str
    estimate: FiniteFloat


class FactorModelComparison(DomainModel):
    name: str
    factor_ids: tuple[FactorId, ...]
    sample_count: int
    r_squared: FiniteFloat
    adjusted_r_squared: FiniteFloat
    residual_volatility: FiniteFloat
    condition_number: FiniteFloat
    variance_inflation_factors: tuple[FiniteFloat, ...]
    high_collinearity: bool
    coefficients: tuple[FactorModelCoefficient, ...]


class FactorStability(DomainModel):
    term: FactorId
    label: str
    window_count: int
    minimum: FiniteFloat
    maximum: FiniteFloat
    median: FiniteFloat
    latest: FiniteFloat
    latest_end_date: date
    latest_is_current: bool
    standard_deviation: FiniteFloat | None = None
    positive_fraction: FiniteFloat = Field(ge=0, le=1)
    negative_fraction: FiniteFloat = Field(ge=0, le=1)


class FactorRelationship(DomainModel):
    term: FactorId
    label: str
    univariate_beta: FiniteFloat
    univariate_r_squared: FiniteFloat = Field(ge=0, le=1)
    pearson: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    spearman: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    partial_correlation: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    incremental_r_squared: FiniteFloat = Field(ge=0, le=1)


class RollingFactorCorrelation(DomainModel):
    start_date: date
    end_date: date
    pearson: tuple[FiniteFloat | None, ...]
    spearman: tuple[FiniteFloat | None, ...]


class FactorResidualDiagnostics(DomainModel):
    lag: int = Field(ge=1, le=60)
    durbin_watson: FiniteFloat | None = Field(default=None, ge=0, le=4)
    ljung_box_statistic: FiniteFloat | None = Field(default=None, ge=0)
    ljung_box_p_value: FiniteFloat | None = Field(default=None, ge=0, le=1)
    jarque_bera_statistic: FiniteFloat | None = Field(default=None, ge=0)
    jarque_bera_p_value: FiniteFloat | None = Field(default=None, ge=0, le=1)
    residual_skew: FiniteFloat | None = None
    residual_kurtosis: FiniteFloat | None = Field(default=None, ge=0)
    arch_lm_statistic: FiniteFloat | None = Field(default=None, ge=0)
    arch_lm_p_value: FiniteFloat | None = Field(default=None, ge=0, le=1)


class FactorStudyPreviewColumn(DomainModel):
    term: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=96)
    unit: str = Field(min_length=1, max_length=48)


class FactorStudyPreviewRow(DomainModel):
    start_date: date
    end_date: date
    values: tuple[FiniteFloat, ...] = Field(max_length=MAX_FACTOR_COUNT + 1)


class FactorStudyPreview(DomainModel):
    """In-memory aligned model inputs; never embedded in a persisted research report."""

    schema_version: Literal["factor-study-preview-v1"] = "factor-study-preview-v1"
    columns: tuple[FactorStudyPreviewColumn, ...] = Field(
        min_length=2, max_length=MAX_FACTOR_COUNT + 1
    )
    rows: tuple[FactorStudyPreviewRow, ...] = Field(max_length=4096)
    inputs: tuple[FactorInputSummary, ...] = Field(default=(), max_length=MAX_FACTOR_INPUTS)
    datasets: tuple[FactorDatasetSummary, ...] = Field(
        default=(), max_length=MAX_FACTOR_INPUTS + 1
    )
    coverage: tuple[FactorCoverage, ...] = Field(default=(), max_length=MAX_FACTOR_INPUTS + 1)

    @model_validator(mode="after")
    def validate_row_width(self) -> Self:
        if any(len(row.values) != len(self.columns) for row in self.rows):
            raise ValueError("row values must match preview columns")
        return self


class FactorRegressionPresentation(DomainModel):
    schema_version: Literal["factor-regression-v5"] = "factor-regression-v5"
    purpose: Literal["historical_explanation"] = "historical_explanation"
    return_mode: Literal["raw_total_return", "excess_return"] = "raw_total_return"
    preset: Literal["us_etf", "msci_europe", "custom", "french"]
    frequency: FactorFrequency = "daily"
    study_currency: str | None = None
    region: FactorRegion = "US"
    requested_start: date
    requested_end: date
    actual_start: date | None = None
    actual_end: date | None = None
    sample_count: int = Field(default=0, ge=0, le=4096)
    dropped_interval_count: int = Field(default=0, ge=0, le=20480)
    discontinuity_count: int = Field(default=0, ge=0, le=4095)
    minimum_observations: int = Field(ge=60, le=2520)
    hac_lags: int = Field(ge=0, le=60)
    covariance: Literal["HAC_bartlett_small_sample_t", "withheld_irregular_spacing"] = (
        "HAC_bartlett_small_sample_t"
    )
    coefficients: tuple[FactorCoefficient, ...] = Field(default=(), max_length=MAX_FACTOR_COUNT + 1)
    r_squared: FiniteFloat | None = None
    adjusted_r_squared: FiniteFloat | None = None
    joint_factor_f_statistic: FiniteFloat | None = Field(default=None, ge=0)
    joint_factor_p_value: FiniteFloat | None = Field(default=None, ge=0, le=1)
    residual_volatility: FiniteFloat | None = None
    condition_number: FiniteFloat | None = None
    factor_correlations: tuple[tuple[FiniteFloat, ...], ...] = Field(
        default=(), max_length=MAX_FACTOR_COUNT
    )
    variance_inflation_factors: tuple[FiniteFloat, ...] = Field(
        default=(), max_length=MAX_FACTOR_COUNT
    )
    diagnostics: tuple[FactorDiagnostic, ...] = ()
    inputs: tuple[FactorInputSummary, ...] = Field(default=(), max_length=MAX_FACTOR_INPUTS)
    datasets: tuple[FactorDatasetSummary, ...] = Field(default=(), max_length=MAX_FACTOR_INPUTS + 1)
    coverage: tuple[FactorCoverage, ...] = Field(default=(), max_length=MAX_FACTOR_INPUTS + 1)
    comparisons: tuple[FactorModelComparison, ...] = Field(default=(), max_length=9)
    residualizations: tuple[FactorResidualizationSummary, ...] = Field(
        default=(), max_length=MAX_FACTOR_COUNT
    )
    stability: tuple[FactorStability, ...] = Field(default=(), max_length=MAX_FACTOR_COUNT)
    rolling_window: int = 252
    rolling: tuple[RollingFactorFit, ...] = Field(default=(), max_length=121)
    rolling_skipped_end_dates: tuple[date, ...] = Field(default=(), max_length=121)
    residual_diagnostics: FactorResidualDiagnostics | None = None
    influential_count: int = 0
    original_factor_correlations: tuple[tuple[FiniteFloat, ...], ...] = Field(
        default=(), max_length=MAX_FACTOR_COUNT
    )
    relationships: tuple[FactorRelationship, ...] = Field(default=(), max_length=MAX_FACTOR_COUNT)
    rolling_correlations: tuple[RollingFactorCorrelation, ...] = Field(default=(), max_length=121)
    attribution_mode: Literal["original", "controls", "sequential"] = "original"
    sequential_order: tuple[FactorId, ...] = Field(default=(), max_length=MAX_FACTOR_COUNT)
    configuration_ref: OpaqueReference
    error_stage: str | None = None
    error_code: str | None = None
    error_id: str | None = None
    numerical_library: Literal["statsmodels_0_15_0"] = "statsmodels_0_15_0"


class CrossSectionQuantileReturn(DomainModel):
    quantile: int = Field(ge=1, le=10)
    asset_count: int = Field(ge=1, le=64)
    mean_forward_return: FiniteFloat


class CrossSectionQuantileSummary(DomainModel):
    quantile: int = Field(ge=1, le=10)
    period_count: int = Field(ge=1, le=121)
    mean_forward_return: FiniteFloat


class CrossSectionPeriod(DomainModel):
    formation_date: date
    eligible_asset_count: int = Field(ge=0, le=64)
    rank_ic: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    quantile_returns: tuple[CrossSectionQuantileReturn, ...] = Field(
        default=(), max_length=10
    )
    top_minus_bottom: FiniteFloat | None = None
    top_turnover: FiniteFloat | None = Field(default=None, ge=0, le=1)
    top_return: FiniteFloat | None = Field(default=None, ge=-1)
    net_top_return: FiniteFloat | None = Field(default=None, ge=-1)
    universe_return: FiniteFloat | None = Field(default=None, ge=-1)
    top_excess_return: FiniteFloat | None = None
    diagnostics: tuple[
        Literal[
            "insufficient_assets",
            "insufficient_group_assets",
            "insufficient_distinct_scores",
            "constant_forward_returns",
        ],
        ...,
    ] = ()


class CrossSectionAssetCoverage(DomainModel):
    instrument: InstrumentId
    sector: str | None = None
    country: str | None = None
    member_from: date | None = None
    member_to: date | None = None
    monthly_period_count: int = Field(ge=0, le=4096)
    evaluated_period_count: int = Field(ge=0, le=121)


class CrossSectionSegmentSummary(DomainModel):
    name: Literal["development", "holdout"]
    start_date: date
    end_date: date
    period_count: int = Field(ge=0, le=121)
    purged_period_count: int = Field(default=0, ge=0, le=1)
    average_rank_ic: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    median_rank_ic: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    rank_ic_information_ratio: FiniteFloat | None = None
    positive_rank_ic_fraction: FiniteFloat | None = Field(default=None, ge=0, le=1)
    average_top_excess_return: FiniteFloat | None = None
    annualized_net_top_return: FiniteFloat | None = Field(default=None, ge=-1)
    annualized_universe_return: FiniteFloat | None = Field(default=None, ge=-1)


class CrossSectionalSignalPresentation(DomainModel):
    schema_version: Literal["cross-sectional-signal-v2"] = "cross-sectional-signal-v2"
    purpose: Literal["predictive_signal_evaluation"] = "predictive_signal_evaluation"
    signal: Literal["momentum_12_1"] = "momentum_12_1"
    formation_frequency: Literal["monthly"] = "monthly"
    entry_convention: Literal["next_session_close"] = "next_session_close"
    holding_period: Literal["next_rebalance_close"] = "next_rebalance_close"
    quantile_count: int = Field(ge=3, le=10)
    minimum_assets: int = Field(ge=3, le=64)
    universe_mode: Literal["current_watchlist", "point_in_time"] = "current_watchlist"
    grouping: Literal["overall", "sector", "country", "sector_country"] = "overall"
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    return_basis: Literal["gross_total_return", "net_total_return", "adjusted_close_return"]
    requested_start: date
    requested_end: date
    average_rank_ic: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    median_rank_ic: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    rank_ic_standard_deviation: FiniteFloat | None = Field(default=None, ge=0)
    rank_ic_information_ratio: FiniteFloat | None = None
    positive_rank_ic_fraction: FiniteFloat | None = Field(default=None, ge=0, le=1)
    rank_ic_lower_95: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    rank_ic_upper_95: FiniteFloat | None = Field(default=None, ge=-1, le=1)
    average_top_minus_bottom: FiniteFloat | None = None
    average_top_turnover: FiniteFloat | None = Field(default=None, ge=0, le=1)
    average_top_return: FiniteFloat | None = Field(default=None, ge=-1)
    average_net_top_return: FiniteFloat | None = Field(default=None, ge=-1)
    average_universe_return: FiniteFloat | None = Field(default=None, ge=-1)
    average_top_excess_return: FiniteFloat | None = None
    annualized_net_top_return: FiniteFloat | None = Field(default=None, ge=-1)
    annualized_universe_return: FiniteFloat | None = Field(default=None, ge=-1)
    annualized_net_top_volatility: FiniteFloat | None = Field(default=None, ge=0)
    net_top_max_drawdown: FiniteFloat | None = Field(default=None, ge=-1, le=0)
    transaction_cost_bps: FiniteFloat = Field(default=0, ge=0, le=500)
    average_quantile_returns: tuple[CrossSectionQuantileSummary, ...] = Field(
        default=(), max_length=10
    )
    periods: tuple[CrossSectionPeriod, ...] = Field(default=(), max_length=121)
    segments: tuple[CrossSectionSegmentSummary, ...] = Field(default=(), max_length=2)
    coverage: tuple[CrossSectionAssetCoverage, ...] = Field(default=(), max_length=64)
    diagnostics: tuple[
        Literal[
            "current_universe_only",
            "survivorship_bias_uncontrolled",
            "transaction_costs_excluded",
            "transaction_costs_illustrative",
            "terminal_returns_unverified",
            "holdout_not_configured",
            "discontinuous_evaluation_periods",
            "insufficient_valid_periods",
        ],
        ...,
    ] = ()
    configuration_ref: OpaqueReference


class AnalystResult(DomainModel):
    """The output from one independently selected analyst."""

    analyst: AnalystName
    instrument: InstrumentId
    summary: NonEmptyText
    status: ReportStatus = ReportStatus.COMPLETE
    missing_metrics: tuple[MetricKind, ...] = ()
    failure_category: FailureCategory = FailureCategory.NONE
    limitations: tuple[LimitationKind, ...] = ()
    methods: tuple[AnalysisMethod, ...] = ()
    inference: InferenceKind = InferenceKind.FACTOR_ANALYSIS
    signal: SignalKind = SignalKind.NOT_ASSESSED
    citations: tuple[Citation, ...] = ()
    observations: tuple[Observation, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    presentation: (
        WorthBuyPresentation
        | MarkovPresentation
        | CorrelationPresentation
        | AssetAllocationPresentation
        | BacktestPresentation
        | SignalEvaluationPresentation
        | PriceActionStructurePresentation
        | FactorRegressionPresentation
        | CrossSectionalSignalPresentation
        | None
    ) = None


class ResearchReport(DomainModel):
    """A typed report schema common to native and containerized deployments."""

    request_id: UUID
    instrument: InstrumentId
    results: tuple[AnalystResult, ...]
    generated_at: datetime
