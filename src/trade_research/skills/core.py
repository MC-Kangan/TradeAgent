"""Immutable research skills and the small composition layer that invokes them."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, cast

from trade_research.domain import AnalystResult, InstrumentId, Observation
from trade_research.providers import (
    FundamentalProvider,
    PricePoint,
    PriceProvider,
    ProviderRegistry,
)


class ResearchSkill(Protocol):
    """An immutable analyst capability selected by its stable name."""

    name: str

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult: ...


@dataclass(frozen=True, slots=True, init=False)
class SkillRegistry:
    """Discover frozen skill definitions without runtime registration or mutation."""

    _skills: Mapping[str, ResearchSkill]
    _names: tuple[str, ...]

    def __init__(self, skills: Iterable[ResearchSkill]) -> None:
        skill_items = tuple(skills)
        for skill in skill_items:
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

    name: ClassVar[str] = "fundamental"

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        provider = cast(FundamentalProvider, providers.require("fundamentals"))
        observations = provider.fundamentals(instrument)
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
        )
        _append_ratio(factors, missing, instrument, "net_margin", earnings, revenue)
        _append_ratio(factors, missing, instrument, "return_on_equity", earnings, equity)
        if free_cash_flow is None:
            missing.append("free_cash_flow")
        else:
            factors.append(
                _factor(
                    instrument, "free_cash_flow", _numeric_value(free_cash_flow), (free_cash_flow,)
                )
            )
        _append_ratio(
            factors,
            missing,
            instrument,
            "free_cash_flow_margin",
            free_cash_flow,
            revenue,
        )
        _append_ratio(factors, missing, instrument, "leverage", debt, equity)
        _append_ratio(
            factors,
            missing,
            instrument,
            "price_to_earnings",
            market_cap,
            earnings,
        )
        _append_ratio(
            factors,
            missing,
            instrument,
            "enterprise_value_to_ebitda",
            enterprise_value,
            ebitda,
        )
        _append_ratio(
            factors,
            missing,
            instrument,
            "free_cash_flow_yield",
            free_cash_flow,
            market_cap,
        )

        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=_summary(missing),
            observations=tuple(factors),
        )


@dataclass(frozen=True, slots=True)
class TechnicalSkill:
    """Calculate deterministic indicators from sorted, validated OHLCV history."""

    name: ClassVar[str] = "technical"
    window: int = 20
    RSI_WINDOW: ClassVar[int] = 14
    MACD_FAST: ClassVar[int] = 12
    MACD_SLOW: ClassVar[int] = 26
    MACD_SIGNAL: ClassVar[int] = 9
    ATR_WINDOW: ClassVar[int] = 14
    MOMENTUM_WINDOW: ClassVar[int] = 10
    VOLATILITY_WINDOW: ClassVar[int] = 20
    VOLUME_WINDOW: ClassVar[int] = 20
    BOLLINGER_DEVIATIONS: ClassVar[float] = 2.0
    ANNUALIZATION_DAYS: ClassVar[int] = 252

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError("technical window must be at least two observations")

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        provider = cast(PriceProvider, providers.require("prices"))
        supplied = provider.price_history(instrument)
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

        if len(prices) >= self.window:
            average_prices = prices[-self.window :]
            sma = statistics.fmean(point.close for point in average_prices)
            _append_price_factor(
                factors,
                instrument,
                "simple_moving_average",
                sma,
                average_prices,
                "close",
                f"{self.window}_observations",
            )
            _append_price_factor(
                factors,
                instrument,
                f"simple_moving_average_{self.window}",
                sma,
                average_prices,
                "close",
                f"{self.window}_observations",
            )
            ema = _ema_series([point.close for point in prices], self.window)[-1]
            _append_price_factor(
                factors,
                instrument,
                f"exponential_moving_average_{self.window}",
                ema,
                prices,
                "close",
                f"{self.window}_observations",
            )
            deviation = statistics.pstdev(point.close for point in average_prices)
            _append_price_factor(
                factors,
                instrument,
                f"bollinger_middle_{self.window}",
                sma,
                average_prices,
                "close",
                f"{self.window}_observations",
            )
            _append_price_factor(
                factors,
                instrument,
                f"bollinger_upper_{self.window}_2",
                sma + self.BOLLINGER_DEVIATIONS * deviation,
                average_prices,
                "close",
                f"{self.window}_observations_2_standard_deviations",
            )
            _append_price_factor(
                factors,
                instrument,
                f"bollinger_lower_{self.window}_2",
                sma - self.BOLLINGER_DEVIATIONS * deviation,
                average_prices,
                "close",
                f"{self.window}_observations_2_standard_deviations",
            )
        else:
            missing.extend(
                ("simple_moving_average", "exponential_moving_average", "bollinger_bands")
            )

        if len(prices) >= self.RSI_WINDOW + 1:
            _append_price_factor(
                factors,
                instrument,
                f"relative_strength_index_{self.RSI_WINDOW}",
                _rsi([point.close for point in prices], self.RSI_WINDOW),
                prices,
                "close",
                f"wilder_{self.RSI_WINDOW}_observations",
            )
        else:
            missing.append("relative_strength_index")

        macd_values = _macd_series(
            [point.close for point in prices], self.MACD_FAST, self.MACD_SLOW
        )
        if len(macd_values) >= self.MACD_SIGNAL:
            signal_values = _ema_series(macd_values, self.MACD_SIGNAL)
            macd = macd_values[-1]
            signal = signal_values[-1]
            _append_price_factor(
                factors,
                instrument,
                f"macd_{self.MACD_FAST}_{self.MACD_SLOW}",
                macd,
                prices,
                "close",
                f"ema_{self.MACD_FAST}_minus_ema_{self.MACD_SLOW}",
            )
            _append_price_factor(
                factors,
                instrument,
                f"macd_signal_{self.MACD_SIGNAL}",
                signal,
                prices,
                "close",
                f"ema_{self.MACD_SIGNAL}_of_macd",
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
        if len(ohlcv_prices) >= self.ATR_WINDOW:
            _append_price_factor(
                factors,
                instrument,
                f"average_true_range_{self.ATR_WINDOW}",
                _atr(ohlcv_prices, self.ATR_WINDOW),
                ohlcv_prices,
                "ohlcv",
                f"wilder_{self.ATR_WINDOW}_observations",
            )
        else:
            missing.append("average_true_range")

        if len(prices) >= self.MOMENTUM_WINDOW + 1:
            momentum_prices = (prices[-self.MOMENTUM_WINDOW - 1], prices[-1])
            _append_price_factor(
                factors,
                instrument,
                f"momentum_{self.MOMENTUM_WINDOW}",
                momentum_prices[-1].close / momentum_prices[0].close - 1,
                momentum_prices,
                "close",
                f"{self.MOMENTUM_WINDOW}_period_return",
            )
        else:
            missing.append("momentum")

        if len(prices) >= self.VOLATILITY_WINDOW + 1:
            volatility_prices = prices[-self.VOLATILITY_WINDOW - 1 :]
            returns = [
                volatility_prices[index].close / volatility_prices[index - 1].close - 1
                for index in range(1, len(volatility_prices))
            ]
            volatility = statistics.stdev(returns) * math.sqrt(self.ANNUALIZATION_DAYS)
            _append_price_factor(
                factors,
                instrument,
                f"annualized_volatility_{self.VOLATILITY_WINDOW}",
                volatility,
                volatility_prices,
                "close",
                f"{self.VOLATILITY_WINDOW}_returns_sqrt_{self.ANNUALIZATION_DAYS}",
            )
        else:
            missing.append("annualized_volatility")

        volume_prices = tuple(point for point in prices if point.volume is not None)
        if len(volume_prices) >= self.VOLUME_WINDOW * 2:
            volume_window_prices = volume_prices[-self.VOLUME_WINDOW * 2 :]
            prior_average = statistics.fmean(
                cast(float, point.volume) for point in volume_window_prices[: self.VOLUME_WINDOW]
            )
            current_average = statistics.fmean(
                cast(float, point.volume) for point in volume_window_prices[self.VOLUME_WINDOW :]
            )
            if prior_average != 0:
                _append_price_factor(
                    factors,
                    instrument,
                    f"volume_trend_{self.VOLUME_WINDOW}",
                    current_average / prior_average - 1,
                    volume_window_prices,
                    "volume",
                    f"last_{self.VOLUME_WINDOW}_versus_prior_{self.VOLUME_WINDOW}",
                )
            else:
                missing.append("volume_trend non-zero prior volume")
        else:
            missing.append("volume_trend")

        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=_summary(missing),
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
            summary = result.summary
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
            and (period is None or observation.provenance.get("period") == period)
        ]
        if not candidates and period == "current":
            candidates = [
                observation
                for observation in observations
                if observation.metric == metric
                and _is_finite_number(observation.value)
                and "period" not in observation.provenance
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
    value = (_numeric_value(current) - _numeric_value(prior)) / _numeric_value(prior)
    factors.append(_factor(instrument, metric, value, (current, prior)))


def _append_ratio(
    factors: list[Observation],
    missing: list[str],
    instrument: InstrumentId,
    metric: str,
    numerator: Observation | None,
    denominator: Observation | None,
) -> None:
    if numerator is None or denominator is None or _numeric_value(denominator) == 0:
        missing.append(metric)
        return
    factors.append(
        _factor(
            instrument,
            metric,
            _numeric_value(numerator) / _numeric_value(denominator),
            (numerator, denominator),
        )
    )


def _factor(
    instrument: InstrumentId,
    metric: str,
    value: float,
    inputs: tuple[Observation, ...],
) -> Observation:
    return Observation(
        instrument=instrument,
        metric=metric,
        value=round(value, 10),
        source="fundamental-skill",
        observed_at=max(item.observed_at for item in inputs),
        provenance={"inputs": [_observation_input(item) for item in inputs]},
    )


def _observation_input(observation: Observation) -> dict[str, object]:
    period = observation.provenance.get("period")
    provider_reference = {
        key: value for key, value in observation.provenance.items() if key != "period"
    }
    return {
        "metric": observation.metric,
        "period": period,
        "observed_at": observation.observed_at.isoformat(),
        "value": observation.value,
        "source": observation.source,
        "provider_reference": provider_reference,
    }


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
    factors.append(
        Observation(
            instrument=instrument,
            metric=metric,
            value=round(value, 10),
            source="technical-skill",
            observed_at=max(point.observed_at for point in prices),
            provenance={
                "lookback": lookback,
                "inputs": [_price_input(point, input_metric) for point in prices],
            },
        )
    )


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
        "source": point.source,
        "provider_reference": dict(point.provenance),
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


def _summary(missing: Sequence[str]) -> str:
    if missing:
        return f"partial data: missing {', '.join(dict.fromkeys(missing))}"
    return "complete data: all required inputs available"
