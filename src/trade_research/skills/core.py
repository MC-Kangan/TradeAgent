"""Immutable research skills and the small composition layer that invokes them."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any, Protocol, cast

from trade_research.domain import (
    AnalystResult,
    Evidence,
    InstrumentId,
    LimitationKind,
    MetricKind,
    Observation,
    ReportStatus,
)
from trade_research.domain.provenance import (
    MAX_PROVENANCE_ITEMS,
    normalize_provider_kind,
    sanitize_provider_reference,
)
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry


class ResearchSkill(Protocol):
    """An immutable analyst capability selected by its stable name."""

    name: str
    required_capabilities: tuple[CapabilityName, ...]

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult: ...


@dataclass(frozen=True, slots=True, init=False)
class SkillRegistry:
    """Discover frozen skill definitions without runtime registration or mutation."""

    _skills: Mapping[str, ResearchSkill]
    _names: tuple[str, ...]

    def __init__(self, skills: Iterable[ResearchSkill]) -> None:
        skill_items = tuple(skills)
        for skill in skill_items:
            required_capabilities = getattr(skill, "required_capabilities", None)
            if not isinstance(required_capabilities, tuple) or any(
                not isinstance(capability, CapabilityName)
                for capability in required_capabilities
            ):
                raise TypeError(
                    f"skill '{skill.name}' required_capabilities must be a tuple of CapabilityName"
                )
            if len(set(required_capabilities)) != len(required_capabilities):
                raise TypeError(f"skill '{skill.name}' declares duplicate required_capabilities")
            parameters = getattr(type(skill), "__dataclass_params__", None)
            if parameters is None or not parameters.frozen:
                raise TypeError(f"skill '{skill.name}' must be a frozen dataclass")
            if not all(
                _is_deeply_immutable(getattr(skill, item.name)) for item in fields(cast(Any, skill))
            ):
                raise TypeError(f"skill '{skill.name}' must contain only immutable fields")
        discovered = {skill.name: skill for skill in skill_items}
        if len(discovered) != len(skill_items):
            raise ValueError("skill names must be unique")
        object.__setattr__(self, "_skills", MappingProxyType(discovered))
        object.__setattr__(self, "_names", tuple(discovered))

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    def require(self, name: str) -> ResearchSkill:
        try:
            return self._skills[name]
        except KeyError as error:
            raise KeyError(name) from error

    def discover(self, selected: Sequence[str]) -> tuple[ResearchSkill, ...]:
        return tuple(self.require(name) for name in selected)


@dataclass(frozen=True, slots=True)
class FundamentalSkill:
    """Calculate the required growth, quality, cash-flow, and valuation factors."""

    _name: str = field(default="fundamental", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.FUNDAMENTALS,)

    def __getattribute__(self, attribute: str) -> object:
        if attribute == "name":
            return object.__getattribute__(self, "_name")
        return object.__getattribute__(self, attribute)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        observations = providers.fundamentals(instrument)
        factors: list[Observation] = []
        missing: list[str] = []

        revenue = _select_metric(observations, ("revenue",), "current")
        prior_revenue = _select_metric(observations, ("revenue",), "prior")
        earnings = _select_metric(observations, ("net_income", "earnings"), "current")
        prior_earnings = _select_metric(observations, ("net_income", "earnings"), "prior")
        operating_income = _select_metric(observations, ("operating_income",), "current")
        equity = _select_metric(observations, ("shareholders_equity", "total_equity"), "current")
        free_cash_flow = _select_metric(observations, ("free_cash_flow",), "current")
        debt = _select_metric(observations, ("total_debt",), "current")
        market_cap = _select_metric(observations, ("market_cap",), None)
        enterprise_value = _select_metric(observations, ("enterprise_value",), None)
        ebitda = _select_metric(observations, ("ebitda",), "current")

        _append_growth(
            factors,
            missing,
            instrument,
            "revenue_growth",
            revenue,
            prior_revenue,
        )
        _append_growth(
            factors,
            missing,
            instrument,
            "earnings_growth",
            earnings,
            prior_earnings,
        )
        _append_ratio(
            factors,
            missing,
            instrument,
            "operating_margin",
            operating_income,
            revenue,
            compatibility="statement",
        )
        _append_ratio(
            factors,
            missing,
            instrument,
            "net_margin",
            earnings,
            revenue,
            compatibility="statement",
        )
        _append_ratio(
            factors,
            missing,
            instrument,
            "return_on_equity",
            earnings,
            equity,
            compatibility="statement",
        )
        if free_cash_flow is None or _statement_context(free_cash_flow) is None:
            missing.append("free_cash_flow incompatible input metadata")
        else:
            factors.append(
                _factor(
                    instrument,
                    "free_cash_flow",
                    _numeric_value(free_cash_flow),
                    (free_cash_flow,),
                    algorithm="direct_value",
                    window="current_period",
                )
            )
        _append_ratio(
            factors,
            missing,
            instrument,
            "free_cash_flow_margin",
            free_cash_flow,
            revenue,
            compatibility="statement",
        )
        _append_ratio(
            factors,
            missing,
            instrument,
            "leverage",
            debt,
            equity,
            compatibility="statement",
        )
        _append_ratio(
            factors,
            missing,
            instrument,
            "price_to_earnings",
            market_cap,
            earnings,
            compatibility="valuation",
        )
        _append_ratio(
            factors,
            missing,
            instrument,
            "enterprise_value_to_ebitda",
            enterprise_value,
            ebitda,
            compatibility="valuation",
        )
        _append_ratio(
            factors,
            missing,
            instrument,
            "free_cash_flow_yield",
            free_cash_flow,
            market_cap,
            compatibility="valuation_inverse",
        )

        return AnalystResult(
            analyst=self._name,
            instrument=instrument,
            summary=_summary(missing),
            status=ReportStatus.PARTIAL if missing else ReportStatus.COMPLETE,
            missing_metrics=_missing_metric_kinds(missing),
            limitations=_limitations(missing),
            observations=tuple(factors),
        )


@dataclass(frozen=True, slots=True)
class TechnicalSkill:
    """Calculate deterministic indicators from sorted, validated OHLCV history."""

    window: int = 20
    _name: str = field(default="technical", init=False, repr=False)
    _window: int = field(init=False, repr=False)
    _rsi_window: int = field(default=14, init=False, repr=False)
    _macd_fast: int = field(default=12, init=False, repr=False)
    _macd_slow: int = field(default=26, init=False, repr=False)
    _macd_signal: int = field(default=9, init=False, repr=False)
    _atr_window: int = field(default=14, init=False, repr=False)
    _momentum_window: int = field(default=10, init=False, repr=False)
    _volatility_window: int = field(default=20, init=False, repr=False)
    _volume_window: int = field(default=20, init=False, repr=False)
    _bollinger_deviations: float = field(default=2.0, init=False, repr=False)
    _annualization_days: int = field(default=252, init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.PRICES,)

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError("technical window must be at least two observations")
        object.__setattr__(self, "_window", self.window)

    def __getattribute__(self, attribute: str) -> object:
        if attribute == "name":
            return object.__getattribute__(self, "_name")
        return object.__getattribute__(self, attribute)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        supplied = providers.prices(instrument)
        prices, discarded, incomplete_ohlcv = _validated_prices(supplied)
        factors: list[Observation] = []
        missing: list[str] = []

        if discarded:
            missing.append("discarded invalid or duplicate OHLCV")
        if incomplete_ohlcv:
            missing.append("complete OHLCV fields")

        if len(prices) >= 2:
            return_prices = (prices[0], prices[-1])
            _append_price_factor(
                factors,
                instrument,
                "price_return",
                prices[-1].close / prices[0].close - 1,
                return_prices,
                "close",
                "full_history",
            )
        else:
            missing.append("price_return")

        if len(prices) >= self._window:
            average_prices = prices[-self._window :]
            sma = statistics.fmean(point.close for point in average_prices)
            _append_price_factor(
                factors,
                instrument,
                "simple_moving_average",
                sma,
                average_prices,
                "close",
                f"{self._window}_observations",
            )
            if self._window == 20:
                _append_price_factor(
                    factors,
                    instrument,
                    "simple_moving_average_20",
                    sma,
                    average_prices,
                    "close",
                    "20_observations",
                )
                ema = _ema_series([point.close for point in prices], 20)[-1]
                _append_price_factor(
                    factors,
                    instrument,
                    "exponential_moving_average_20",
                    ema,
                    prices,
                    "close",
                    "20_observations",
                )
                deviation = statistics.pstdev(point.close for point in average_prices)
                _append_price_factor(
                    factors,
                    instrument,
                    "bollinger_middle_20",
                    sma,
                    average_prices,
                    "close",
                    "20_observations",
                )
                _append_price_factor(
                    factors,
                    instrument,
                    "bollinger_upper_20_2",
                    sma + self._bollinger_deviations * deviation,
                    average_prices,
                    "close",
                    "20_observations_2_standard_deviations",
                )
                _append_price_factor(
                    factors,
                    instrument,
                    "bollinger_lower_20_2",
                    sma - self._bollinger_deviations * deviation,
                    average_prices,
                    "close",
                    "20_observations_2_standard_deviations",
                )
            else:
                missing.extend(("ema_20", "bollinger_bands_20"))
        else:
            missing.extend(
                ("simple_moving_average", "exponential_moving_average", "bollinger_bands")
            )

        if len(prices) >= self._rsi_window + 1:
            _append_price_factor(
                factors,
                instrument,
                f"relative_strength_index_{self._rsi_window}",
                _rsi([point.close for point in prices], self._rsi_window),
                prices,
                "close",
                f"wilder_{self._rsi_window}_observations",
            )
        else:
            missing.append("relative_strength_index")

        macd_values = _macd_series(
            [point.close for point in prices], self._macd_fast, self._macd_slow
        )
        if len(macd_values) >= self._macd_signal:
            signal_values = _ema_series(macd_values, self._macd_signal)
            macd = macd_values[-1]
            signal = signal_values[-1]
            _append_price_factor(
                factors,
                instrument,
                f"macd_{self._macd_fast}_{self._macd_slow}",
                macd,
                prices,
                "close",
                f"ema_{self._macd_fast}_minus_ema_{self._macd_slow}",
            )
            _append_price_factor(
                factors,
                instrument,
                f"macd_signal_{self._macd_signal}",
                signal,
                prices,
                "close",
                f"ema_{self._macd_signal}_of_macd",
            )
            _append_price_factor(
                factors,
                instrument,
                "macd_histogram",
                macd - signal,
                prices,
                "close",
                "macd_minus_signal",
            )
        else:
            missing.append("macd")

        ohlcv_prices = tuple(point for point in prices if _has_complete_ohlcv(point))
        if len(ohlcv_prices) >= self._atr_window:
            _append_price_factor(
                factors,
                instrument,
                f"average_true_range_{self._atr_window}",
                _atr(ohlcv_prices, self._atr_window),
                ohlcv_prices,
                "ohlcv",
                f"wilder_{self._atr_window}_observations",
            )
        else:
            missing.append("average_true_range")

        if len(prices) >= self._momentum_window + 1:
            momentum_prices = (prices[-self._momentum_window - 1], prices[-1])
            _append_price_factor(
                factors,
                instrument,
                f"momentum_{self._momentum_window}",
                momentum_prices[-1].close / momentum_prices[0].close - 1,
                momentum_prices,
                "close",
                f"{self._momentum_window}_period_return",
            )
        else:
            missing.append("momentum")

        if len(prices) >= self._volatility_window + 1:
            volatility_prices = prices[-self._volatility_window - 1 :]
            returns = [
                volatility_prices[index].close / volatility_prices[index - 1].close - 1
                for index in range(1, len(volatility_prices))
            ]
            volatility = statistics.stdev(returns) * math.sqrt(self._annualization_days)
            _append_price_factor(
                factors,
                instrument,
                f"annualized_volatility_{self._volatility_window}",
                volatility,
                volatility_prices,
                "close",
                f"{self._volatility_window}_returns_sqrt_{self._annualization_days}",
            )
        else:
            missing.append("annualized_volatility")

        volume_prices = tuple(point for point in prices if point.volume is not None)
        if len(volume_prices) >= self._volume_window * 2:
            volume_window_prices = volume_prices[-self._volume_window * 2 :]
            prior_average = statistics.fmean(
                cast(float, point.volume) for point in volume_window_prices[: self._volume_window]
            )
            current_average = statistics.fmean(
                cast(float, point.volume) for point in volume_window_prices[self._volume_window :]
            )
            if prior_average != 0:
                _append_price_factor(
                    factors,
                    instrument,
                    f"volume_trend_{self._volume_window}",
                    current_average / prior_average - 1,
                    volume_window_prices,
                    "volume",
                    f"last_{self._volume_window}_versus_prior_{self._volume_window}",
                )
            else:
                missing.append("volume_trend non-zero prior volume")
        else:
            missing.append("volume_trend")

        return AnalystResult(
            analyst=self._name,
            instrument=instrument,
            summary=_summary(missing),
            status=ReportStatus.PARTIAL if missing else ReportStatus.COMPLETE,
            missing_metrics=_missing_metric_kinds(missing),
            limitations=_limitations(missing),
            observations=tuple(factors),
        )


@dataclass(frozen=True, slots=True)
class FilingsSkill:
    """Analyze SEC filing history for form-type counts and reporting recency."""

    _name: str = field(default="filings", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.FILINGS,)

    def __getattribute__(self, attribute: str) -> object:
        if attribute == "name":
            return object.__getattribute__(self, "_name")
        return object.__getattribute__(self, attribute)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        evidence = providers.filings(instrument)
        filings = _parse_filing_evidence(evidence)
        factors: list[Observation] = []
        missing: list[str] = []

        if not filings:
            missing.append("no filing data available")
            return AnalystResult(
                analyst=self._name,
                instrument=instrument,
                summary=_summary(missing),
                status=ReportStatus.PARTIAL,
                missing_metrics=_missing_metric_kinds(missing),
                limitations=_limitations(missing),
                observations=(),
            )

        now = datetime.now(tz=UTC)
        ten_k_forms = [f for f in filings if f["form"] in ("10-K", "10-K/A")]
        ten_q_forms = [f for f in filings if f["form"] in ("10-Q", "10-Q/A")]
        eight_k_forms = [f for f in filings if f["form"] in ("8-K", "8-K/A")]

        _append_filing_factor(
            factors, instrument, "recent_filing_count", len(filings),
            filings, algorithm="filing_count", window="all_available",
        )
        _append_filing_factor(
            factors, instrument, "material_event_count", len(eight_k_forms),
            filings, algorithm="filing_count", window="form_8k",
        )

        if ten_k_forms:
            latest_10k = max(ten_k_forms, key=lambda f: cast(date, f["filing_date"]))
            age_days = (now.date() - cast(date, latest_10k["filing_date"])).days
            _append_filing_factor(
                factors, instrument, "annual_report_age_days", age_days,
                filings, algorithm="filing_age", window="latest_10k",
            )
        else:
            missing.append("annual_report_age_days")

        if ten_q_forms:
            latest_10q = max(ten_q_forms, key=lambda f: cast(date, f["filing_date"]))
            age_days = (now.date() - cast(date, latest_10q["filing_date"])).days
            _append_filing_factor(
                factors, instrument, "quarterly_report_age_days", age_days,
                filings, algorithm="filing_age", window="latest_10q",
            )
        else:
            missing.append("quarterly_report_age_days")

        return AnalystResult(
            analyst=self._name,
            instrument=instrument,
            summary=_summary(missing),
            status=ReportStatus.PARTIAL if missing else ReportStatus.COMPLETE,
            missing_metrics=_missing_metric_kinds(missing),
            limitations=_limitations(missing),
            observations=tuple(factors),
        )


class ResearchCompiler:
    """Compile only the user-selected analyst set; no debate topology is imposed."""

    def __init__(self, skills: SkillRegistry, providers: ProviderRegistry) -> None:
        self._skills = skills
        self._providers = providers

    def compile(
        self, instrument: InstrumentId, selected: Sequence[str]
    ) -> tuple[AnalystResult, ...]:
        return tuple(
            skill.analyze(instrument, self._providers) for skill in self._skills.discover(selected)
        )


@dataclass(frozen=True, slots=True)
class ResearchReviewer:
    """Preserve analyst conclusions while making incomplete coverage explicit."""

    def review(self, results: Sequence[AnalystResult]) -> tuple[AnalystResult, ...]:
        reviewed: list[AnalystResult] = []
        for result in results:
            summary = _sanitize_text(result.summary)
            if not result.observations and not summary.startswith("partial data"):
                summary = "partial data: no derived observations"
            reviewed.append(result.model_copy(update={"summary": summary}))
        return tuple(reviewed)


def _select_metric(
    observations: Sequence[Observation], metrics: tuple[str, ...], period: str | None
) -> Observation | None:
    for metric in metrics:
        candidates = [
            observation
            for observation in observations
            if observation.metric == metric
            and _is_finite_number(observation.value)
            and (period is None or observation.provenance.get("period_role") == period)
        ]
        if not candidates and period == "current":
            candidates = [
                observation
                for observation in observations
                if observation.metric == metric
                and _is_finite_number(observation.value)
                and "period_role" not in observation.provenance
            ]
        if candidates:
            return max(candidates, key=lambda observation: observation.observed_at)
    return None


def _append_growth(
    factors: list[Observation],
    missing: list[str],
    instrument: InstrumentId,
    metric: str,
    current: Observation | None,
    prior: Observation | None,
) -> None:
    if current is None or prior is None or _numeric_value(prior) == 0:
        missing.append(metric)
        return
    if not _compatible_growth(current, prior):
        missing.append(f"{metric} incompatible input metadata")
        return
    value = (_numeric_value(current) - _numeric_value(prior)) / _numeric_value(prior)
    factors.append(
        _factor(
            instrument,
            metric,
            value,
            (current, prior),
            algorithm="period_growth",
            window="current_vs_prior",
        )
    )


def _append_ratio(
    factors: list[Observation],
    missing: list[str],
    instrument: InstrumentId,
    metric: str,
    numerator: Observation | None,
    denominator: Observation | None,
    *,
    compatibility: str,
) -> None:
    if numerator is None or denominator is None or _numeric_value(denominator) == 0:
        missing.append(metric)
        return
    if compatibility == "statement":
        compatible = _compatible_statement_ratio(numerator, denominator)
    elif compatibility == "valuation":
        compatible = _compatible_valuation(numerator, denominator)
    elif compatibility == "valuation_inverse":
        compatible = _compatible_valuation(denominator, numerator)
    else:  # pragma: no cover - internal call sites use fixed compatibility modes.
        raise ValueError(f"unknown compatibility mode '{compatibility}'")
    if not compatible:
        missing.append(f"{metric} incompatible input metadata")
        return
    factors.append(
        _factor(
            instrument,
            metric,
            _numeric_value(numerator) / _numeric_value(denominator),
            (numerator, denominator),
            algorithm="ratio",
            window=(
                "same_period" if compatibility == "statement" else "valuation_and_period"
            ),
        )
    )


def _factor(
    instrument: InstrumentId,
    metric: str,
    value: float,
    inputs: tuple[Observation, ...],
    *,
    algorithm: str,
    window: str,
) -> Observation:
    return Observation(
        instrument=instrument,
        metric=metric,
        value=round(value, 10),
        source="derived",
        observed_at=max(item.observed_at for item in inputs),
        provenance={
            "algorithm": algorithm,
            "window": window,
            "inputs": [_observation_input(item) for item in inputs],
        },
    )


def _observation_input(observation: Observation) -> dict[str, object]:
    provider_reference = sanitize_provider_reference(observation.provenance)
    return {
        "metric": observation.metric,
        "period_role": observation.provenance.get("period_role"),
        "period_end": observation.provenance.get("period_end"),
        "period_type": observation.provenance.get("period_type"),
        "period_ref": observation.provenance.get("period_ref"),
        "prior_period_ref": observation.provenance.get("prior_period_ref"),
        "snapshot_ref": observation.provenance.get("snapshot_ref"),
        "currency": observation.provenance.get("currency"),
        "valuation_as_of": observation.provenance.get("valuation_as_of"),
        "observed_at": observation.observed_at.isoformat(),
        "value": observation.value,
        "provider_kind": observation.source.value,
        "provider_reference": provider_reference,
    }


@dataclass(frozen=True, slots=True)
class _StatementContext:
    snapshot_id: str
    period: str
    period_end: date
    period_type: str
    period_id: str
    prior_period_id: str | None
    currency: str


@dataclass(frozen=True, slots=True)
class _ValuationContext:
    snapshot_id: str
    valuation_as_of: datetime
    currency: str


def _statement_context(observation: Observation) -> _StatementContext | None:
    provenance = observation.provenance
    snapshot_id = _metadata_text(provenance.get("snapshot_ref"))
    period = _metadata_text(provenance.get("period_role"))
    period_end_value = _metadata_text(provenance.get("period_end"))
    period_type = _metadata_text(provenance.get("period_type"))
    period_id = _metadata_text(provenance.get("period_ref"))
    currency = _metadata_text(provenance.get("currency"))
    prior_period_id = _metadata_text(provenance.get("prior_period_ref"))
    if (
        snapshot_id is None
        or period not in {"current", "prior"}
        or period_end_value is None
        or period_type not in {"annual", "quarterly", "ttm"}
        or period_id is None
        or currency is None
        or len(currency) != 3
        or not currency.isalpha()
        or currency != currency.upper()
    ):
        return None
    try:
        period_end = date.fromisoformat(period_end_value)
    except ValueError:
        return None
    return _StatementContext(
        snapshot_id=snapshot_id,
        period=period,
        period_end=period_end,
        period_type=period_type,
        period_id=period_id,
        prior_period_id=prior_period_id,
        currency=currency,
    )


def _valuation_context(observation: Observation) -> _ValuationContext | None:
    provenance = observation.provenance
    snapshot_id = _metadata_text(provenance.get("snapshot_ref"))
    valuation_as_of_value = _metadata_text(provenance.get("valuation_as_of"))
    currency = _metadata_text(provenance.get("currency"))
    if (
        snapshot_id is None
        or valuation_as_of_value is None
        or currency is None
        or len(currency) != 3
        or not currency.isalpha()
        or currency != currency.upper()
    ):
        return None
    try:
        valuation_as_of = datetime.fromisoformat(valuation_as_of_value.replace("Z", "+00:00"))
    except ValueError:
        try:
            valuation_as_of = datetime.combine(
                date.fromisoformat(valuation_as_of_value), datetime.min.time(), tzinfo=UTC
            )
        except ValueError:
            return None
    if valuation_as_of.tzinfo is None or observation.observed_at.tzinfo is None:
        return None
    return _ValuationContext(
        snapshot_id=snapshot_id,
        valuation_as_of=valuation_as_of,
        currency=currency,
    )


def _compatible_growth(current: Observation, prior: Observation) -> bool:
    current_context = _statement_context(current)
    prior_context = _statement_context(prior)
    return (
        current_context is not None
        and prior_context is not None
        and current_context.period == "current"
        and prior_context.period == "prior"
        and current_context.snapshot_id == prior_context.snapshot_id
        and current_context.period_type == prior_context.period_type
        and current_context.currency == prior_context.currency
        and current_context.prior_period_id == prior_context.period_id
        and current_context.period_end > prior_context.period_end
    )


def _compatible_statement_ratio(left: Observation, right: Observation) -> bool:
    left_context = _statement_context(left)
    right_context = _statement_context(right)
    if left_context is None or right_context is None:
        return False
    return (
        left_context.period == "current"
        and right_context.period == "current"
        and left_context.snapshot_id == right_context.snapshot_id
        and left_context.period_end == right_context.period_end
        and left_context.period_type == right_context.period_type
        and left_context.period_id == right_context.period_id
        and left_context.currency == right_context.currency
    )


def _compatible_valuation(market_value: Observation, statement_value: Observation) -> bool:
    valuation = _valuation_context(market_value)
    statement = _statement_context(statement_value)
    if valuation is None or statement is None or statement.period != "current":
        return False
    statement_end = datetime.combine(statement.period_end, datetime.min.time(), tzinfo=UTC)
    return (
        valuation.snapshot_id == statement.snapshot_id
        and valuation.currency == statement.currency
        and statement_end <= valuation.valuation_as_of <= market_value.observed_at
    )


def _metadata_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _validated_prices(
    supplied: Sequence[PricePoint],
) -> tuple[tuple[PricePoint, ...], bool, bool]:
    timestamp_counts = Counter(point.observed_at for point in supplied)
    valid: list[PricePoint] = []
    discarded = False
    incomplete_ohlcv = False
    for point in supplied:
        if timestamp_counts[point.observed_at] > 1 or not _valid_price(point):
            discarded = True
            continue
        if not _has_complete_ohlcv(point):
            incomplete_ohlcv = True
        valid.append(point)
    return tuple(sorted(valid, key=lambda point: point.observed_at)), discarded, incomplete_ohlcv


def _valid_price(point: PricePoint) -> bool:
    if point.observed_at.tzinfo is None or not math.isfinite(point.close) or point.close <= 0:
        return False
    optionals = (point.open, point.high, point.low, point.volume)
    if any(value is not None and not math.isfinite(value) for value in optionals):
        return False
    if point.volume is not None and point.volume < 0:
        return False
    if any(value is not None and value <= 0 for value in (point.open, point.high, point.low)):
        return False
    if point.high is not None and point.low is not None and point.high < point.low:
        return False
    if point.high is not None and point.high < point.close:
        return False
    if point.low is not None and point.low > point.close:
        return False
    if point.open is not None and point.high is not None and point.open > point.high:
        return False
    if point.open is not None and point.low is not None and point.open < point.low:
        return False
    return True


def _has_complete_ohlcv(point: PricePoint) -> bool:
    return all(value is not None for value in (point.open, point.high, point.low, point.volume))


def _append_price_factor(
    factors: list[Observation],
    instrument: InstrumentId,
    metric: str,
    value: float,
    prices: Sequence[PricePoint],
    input_metric: str,
    lookback: str,
) -> None:
    provenance: dict[str, object] = {
        "algorithm": _algorithm_for_metric(metric),
        "window": lookback,
        "point_count": len(prices),
        "start_at": min(point.observed_at for point in prices).isoformat(),
        "end_at": max(point.observed_at for point in prices).isoformat(),
        "series_ref": _price_series_reference(prices, input_metric),
        "input_provider_kind": normalize_provider_kind(prices[0].source).value,
    }
    if len(prices) <= MAX_PROVENANCE_ITEMS:
        provenance["inputs"] = [_price_input(point, input_metric) for point in prices]
    factors.append(
        Observation(
            instrument=instrument,
            metric=metric,
            value=round(value, 10),
            source="derived",
            observed_at=max(point.observed_at for point in prices),
            provenance=provenance,
        )
    )


def _algorithm_for_metric(metric: str) -> str:
    if metric == "price_return":
        return "full_history_return"
    if metric.startswith("simple_moving_average"):
        return "simple_moving_average"
    if metric.startswith("exponential_moving_average"):
        return "exponential_moving_average"
    if metric.startswith("relative_strength_index"):
        return "wilder_rsi"
    if metric.startswith("macd_signal"):
        return "macd_signal"
    if metric == "macd_histogram":
        return "macd_histogram"
    if metric.startswith("macd"):
        return "macd"
    if metric.startswith("bollinger"):
        return "bollinger_band"
    if metric.startswith("average_true_range"):
        return "wilder_atr"
    if metric.startswith("momentum"):
        return "momentum"
    if metric.startswith("annualized_volatility"):
        return "annualized_volatility"
    if metric.startswith("volume_trend"):
        return "volume_trend"
    raise ValueError(f"unknown derived price metric '{metric}'")


def _price_series_reference(prices: Sequence[PricePoint], metric: str) -> str:
    values = [_price_input(point, metric) for point in prices]
    canonical = json.dumps(values, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _price_input(point: PricePoint, metric: str) -> dict[str, object]:
    if metric == "ohlcv":
        value: object = {
            "open": point.open,
            "high": point.high,
            "low": point.low,
            "close": point.close,
            "volume": point.volume,
        }
    else:
        value = getattr(point, metric)
    return {
        "metric": metric,
        "timestamp": point.observed_at.isoformat(),
        "value": value,
        "provider_kind": normalize_provider_kind(point.source).value,
        "provider_reference": sanitize_provider_reference(point.provenance),
    }


def _ema_series(values: Sequence[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = statistics.fmean(values[:period])
    result = [ema]
    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
        result.append(ema)
    return result


def _rsi(values: Sequence[float], period: int) -> float:
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    average_gain = statistics.fmean(max(change, 0) for change in changes[:period])
    average_loss = statistics.fmean(max(-change, 0) for change in changes[:period])
    for change in changes[period:]:
        average_gain = (average_gain * (period - 1) + max(change, 0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0)) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100 - 100 / (1 + average_gain / average_loss)


def _macd_series(values: Sequence[float], fast: int, slow: int) -> list[float]:
    if len(values) < slow:
        return []
    fast_values = _ema_series(values, fast)
    slow_values = _ema_series(values, slow)
    fast_offset = slow - fast
    return [fast_values[index + fast_offset] - slow for index, slow in enumerate(slow_values)]


def _atr(prices: Sequence[PricePoint], period: int) -> float:
    true_ranges: list[float] = []
    for index, point in enumerate(prices):
        high = cast(float, point.high)
        low = cast(float, point.low)
        if index == 0:
            true_ranges.append(high - low)
        else:
            previous_close = prices[index - 1].close
            true_ranges.append(
                max(high - low, abs(high - previous_close), abs(low - previous_close))
            )
    average = statistics.fmean(true_ranges[:period])
    for value in true_ranges[period:]:
        average = (average * (period - 1) + value) / period
    return average


def _is_finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_deeply_immutable(value: object) -> bool:
    if value is None or isinstance(value, str | bytes | int | float | bool):
        return True
    if isinstance(value, tuple | frozenset):
        return all(_is_deeply_immutable(item) for item in value)
    parameters = getattr(type(value), "__dataclass_params__", None)
    if parameters is not None and parameters.frozen:
        return all(
            _is_deeply_immutable(getattr(value, item.name)) for item in fields(cast(Any, value))
        )
    return False


def _numeric_value(observation: Observation) -> float:
    value = observation.value
    if not _is_finite_number(value):
        raise TypeError("fundamental factor input must be a finite number")
    return float(cast(int | float, value))


def _sanitize_text(text: str) -> str:
    """Strip control characters and bidi overrides to prevent prompt injection."""
    sanitized: list[str] = []
    for char in text:
        code = ord(char)
        if code < 0x20 and char not in ("\t", "\n"):
            continue
        if 0x7F <= code <= 0x9F:
            continue
        if 0x200B <= code <= 0x200F:
            continue
        if 0x2028 <= code <= 0x202F:
            continue
        if 0x2066 <= code <= 0x2069:
            continue
        if code == 0xFEFF:
            continue
        if 0xFFF0 <= code <= 0xFFFF:
            continue
        sanitized.append(char)
    return "".join(sanitized)


def _summary(missing: Sequence[str]) -> str:
    if missing:
        return _sanitize_text(
            f"partial data: missing {', '.join(dict.fromkeys(missing))}"
        )
    return "complete data: all required inputs available"


def _missing_metric_kinds(missing: Sequence[str]) -> tuple[MetricKind, ...]:
    aliases: dict[str, tuple[MetricKind, ...]] = {
        "ema_20": (MetricKind.EXPONENTIAL_MOVING_AVERAGE_20,),
        "exponential_moving_average": (MetricKind.EXPONENTIAL_MOVING_AVERAGE_20,),
        "bollinger_bands": (
            MetricKind.BOLLINGER_MIDDLE_20,
            MetricKind.BOLLINGER_UPPER_20_2,
            MetricKind.BOLLINGER_LOWER_20_2,
        ),
        "bollinger_bands_20": (
            MetricKind.BOLLINGER_MIDDLE_20,
            MetricKind.BOLLINGER_UPPER_20_2,
            MetricKind.BOLLINGER_LOWER_20_2,
        ),
        "relative_strength_index": (MetricKind.RELATIVE_STRENGTH_INDEX_14,),
        "macd": (MetricKind.MACD_12_26, MetricKind.MACD_SIGNAL_9, MetricKind.MACD_HISTOGRAM),
        "average_true_range": (MetricKind.AVERAGE_TRUE_RANGE_14,),
        "momentum": (MetricKind.MOMENTUM_10,),
        "annualized_volatility": (MetricKind.ANNUALIZED_VOLATILITY_20,),
        "volume_trend": (MetricKind.VOLUME_TREND_20,),
    }
    found: list[MetricKind] = []
    for item in missing:
        prefix = item.split(" ", 1)[0]
        try:
            candidates: tuple[MetricKind, ...] = (MetricKind(prefix),)
        except ValueError:
            candidates = aliases.get(prefix, ())
        for candidate in candidates:
            if candidate not in found:
                found.append(candidate)
    return tuple(found)


def _parse_filing_evidence(
    evidence: Sequence[object],
) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    for item in evidence:
        if not isinstance(item, Evidence):
            continue
        content = item.content
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        form = data.get("form")
        filing_date_str = data.get("filing_date")
        if not isinstance(form, str) or not isinstance(filing_date_str, str):
            continue
        try:
            filing_date = date.fromisoformat(filing_date_str)
        except ValueError:
            continue
        parsed.append({"form": form, "filing_date": filing_date})
    return parsed


def _append_filing_factor(
    factors: list[Observation],
    instrument: InstrumentId,
    metric: str,
    value: float,
    filings: list[dict[str, object]],
    *,
    algorithm: str,
    window: str,
) -> None:
    filing_refs = [
        {"form": str(f["form"]), "filing_date": str(f["filing_date"])}
        for f in filings
    ]
    reference = json.dumps(filing_refs, separators=(",", ":"), sort_keys=True)
    factors.append(
        Observation(
            instrument=instrument,
            metric=metric,
            value=value,
            source="derived",
            observed_at=datetime.now(tz=UTC),
            provenance={
                "algorithm": algorithm,
                "window": window,
                "reference": f"sha256:{hashlib.sha256(reference.encode()).hexdigest()}",
                "input_provider_kind": "sec",
                "point_count": len(filings),
            },
        )
    )


def _limitations(missing: Sequence[str]) -> tuple[LimitationKind, ...]:
    limitations: list[LimitationKind] = []
    joined = " ".join(missing)
    if missing:
        limitations.append(LimitationKind.MISSING_INPUTS)
    if "incompatible" in joined:
        limitations.append(LimitationKind.INCOMPATIBLE_INPUTS)
    if "discarded" in joined:
        limitations.append(LimitationKind.INVALID_ROWS_DISCARDED)
    if "OHLCV" in joined:
        limitations.append(LimitationKind.INCOMPLETE_OHLCV)
    if any(
        word in joined
        for word in ("moving_average", "relative_strength", "macd", "momentum", "volatility")
    ):
        limitations.append(LimitationKind.INSUFFICIENT_HISTORY)
    return tuple(dict.fromkeys(limitations))
