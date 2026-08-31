"""Immutable, serializable values shared by every research interface."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from ipaddress import ip_address
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

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
    tenor: str | None = Field(
        default=None, pattern=r"^[1-9][0-9]{0,2}(?:d|w|m|y)$"
    )
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
                    raise ValueError(
                        "fixed-strike volatility requires strike and no call_delta"
                    )
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
        values = tuple(
            value for value in (self.value, self.high, self.low) if value is not None
        )
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
        partial = [
            (point.high is None) != (point.low is None) for point in self.points
        ]
        if any(partial):
            raise ValueError("outcome high and low must be supplied together")
        if self.barrier_basis == "high_low" and not all(complete):
            raise ValueError("high_low barrier basis requires high and low for every point")
        if self.barrier_basis == "observed_value" and any(complete):
            raise ValueError("observed_value barrier basis cannot include high or low")
        return self


class AnalysisRequest(DomainModel):
    """One bounded instrument or portfolio research request."""

    request_id: UUID = Field(default_factory=uuid4)
    instrument: InstrumentId
    scope: Literal["instrument", "portfolio"] = "instrument"
    portfolio_instruments: tuple[InstrumentId, ...] = Field(default=(), max_length=9)
    analysts: tuple[AnalystName, ...] = Field(
        default=("fundamental", "technical"), min_length=1, max_length=MAX_ANALYSTS
    )
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    positions: tuple[Position, ...] = ()
    skill_parameters: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    price_series: tuple[InlinePriceSeries, ...] = Field(default=(), max_length=9)
    outcome_series: tuple[InlineOutcomeSeries, ...] = Field(default=(), max_length=9)

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
        basket = InstrumentId(symbol="BASKET", market="PORTFOLIO")
        if self.scope == "portfolio":
            if self.instrument != basket:
                raise ValueError("portfolio requests must use the PORTFOLIO:BASKET identity")
            if not 2 <= len(self.portfolio_instruments) <= 9:
                raise ValueError("portfolio requests require between 2 and 9 instruments")
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
        outcome_keys = tuple(
            (item.instrument, item.spec.name) for item in self.outcome_series
        )
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
    return_ratio: float
    duration_bars: int = Field(ge=0)


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
    skipped_signal_count: int = Field(ge=0)
    win_rate: float | None = Field(default=None, ge=0, le=1)
    expected_change: float | None = None
    average_favorable_change: float | None = Field(default=None, ge=0)
    average_adverse_change: float | None = Field(default=None, le=0)


class BacktestExecutionAudit(DomainModel):
    """Aggregate bridge from canonical strategy events to simulated lots."""

    add_signal_count: int = Field(ge=0)
    reduce_signal_count: int = Field(ge=0)
    exit_signal_count: int = Field(ge=0)
    executed_addition_count: int = Field(ge=0)
    unexecuted_addition_count: int = Field(ge=0)


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
    price_adjustment: Literal[
        "raw", "split_adjusted", "split_dividend_adjusted"
    ] | None = None
    daily_boundary: Literal["utc", "exchange_local"] | None = None


class BacktestPositionPerformance(DomainModel):
    """Net result of applying the execution policy to the signal stream."""

    final_equity: float = Field(ge=0)
    total_return: float
    buy_hold_return: float
    max_drawdown: float = Field(ge=0, le=1)
    closed_lot_count: int = Field(ge=0)
    open_lot_count: int = Field(ge=0)
    open_total_size: float = Field(ge=0)
    open_average_entry_price: float | None = Field(default=None, gt=0)
    open_unrealized_pnl: float
    win_rate: float | None = Field(default=None, ge=0, le=1)
    sharpe_ratio: float | None = None


class BacktestPresentation(DomainModel):
    """Bounded, renderer-neutral output for a reproducible backtest."""

    template: Literal["backtesting-v2"] = "backtesting-v2"
    engine: Literal["backtesting.py"] = "backtesting.py"
    engine_version: Annotated[str, Field(min_length=1, max_length=16)]
    strategy_kind: Literal[
        "sma_crossover", "macd_crossover", "rsi_mean_reversion", "markov_regime",
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
    non_overlapping_win_rate_lower_95: float | None = Field(
        default=None, ge=0, le=1
    )
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
    periods: tuple[SignalEvaluationPeriodPresentation, ...] = Field(
        default=(), max_length=3
    )


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
        | None
    ) = None


class ResearchReport(DomainModel):
    """A typed report schema common to native and containerized deployments."""

    request_id: UUID
    instrument: InstrumentId
    results: tuple[AnalystResult, ...]
    generated_at: datetime
