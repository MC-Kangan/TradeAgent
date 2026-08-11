"""Reusable deterministic analytics over normalized daily price series."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from trade_research.domain import (
    AnalysisMethod,
    AnalystResult,
    InstrumentId,
    LimitationKind,
    Observation,
    ReportStatus,
    SignalKind,
)
from trade_research.domain.provenance import DerivedAlgorithm
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.skills.indicators import (
    adx,
    append_price_factor,
    ema_series,
    max_drawdown,
    obv,
    rsi,
    sanitize_text,
    validated_prices,
)

_MIN_TECHNICAL_BARS = 50
_MIN_RISK_RETURNS = 30
_VOLATILITY_WINDOW = 20
_REGIME_LOOKBACK = 120


def _annualization_days(instrument: InstrumentId) -> int:
    return 365 if instrument.market == "CRYPTO" else 252


def _partial(
    instrument: InstrumentId, analyst: str, message: str, limitation: LimitationKind
) -> AnalystResult:
    return AnalystResult(
        analyst=analyst,
        instrument=instrument,
        summary=sanitize_text(message),
        status=ReportStatus.PARTIAL,
        limitations=(limitation,),
        signal=SignalKind.NOT_ASSESSED,
    )


def _simple_returns(closes: Sequence[float]) -> list[float]:
    return [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]


def _log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _moments(values: Sequence[float]) -> tuple[float, float]:
    mean = statistics.fmean(values)
    second = statistics.fmean((value - mean) ** 2 for value in values)
    if second == 0:
        return 0.0, 0.0
    third = statistics.fmean((value - mean) ** 3 for value in values)
    fourth = statistics.fmean((value - mean) ** 4 for value in values)
    return third / second**1.5, fourth / second**2 - 3


def _append(
    observations: list[Observation],
    instrument: InstrumentId,
    metric: str,
    value: float,
    prices: Sequence[PricePoint],
    *,
    input_metric: str = "close",
    lookback: str,
) -> None:
    append_price_factor(observations, instrument, metric, value, prices, input_metric, lookback)


@dataclass(frozen=True, slots=True)
class TechnicalBasicSkill:
    """Composite trend, momentum, volatility-band, and volume confirmation."""

    _name: str = field(default="technical-basic", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.PRICES,)

    def __getattribute__(self, attribute: str) -> object:
        if attribute == "name":
            return object.__getattribute__(self, "_name")
        return object.__getattribute__(self, attribute)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        prices, discarded, incomplete = validated_prices(providers.prices(instrument))
        if incomplete:
            return _partial(
                instrument,
                self.name,
                "Technical confirmation requires complete daily OHLCV bars.",
                LimitationKind.INCOMPLETE_OHLCV,
            )
        if len(prices) < _MIN_TECHNICAL_BARS:
            return _partial(
                instrument,
                self.name,
                f"Technical confirmation requires at least {_MIN_TECHNICAL_BARS} daily bars.",
                LimitationKind.INSUFFICIENT_HISTORY,
            )

        closes = [point.close for point in prices]
        highs = [cast(float, point.high) for point in prices]
        lows = [cast(float, point.low) for point in prices]
        volumes = [cast(float, point.volume) for point in prices]
        ema12 = ema_series(closes, 12)[-1]
        ema26 = ema_series(closes, 26)[-1]
        rsi14 = rsi(closes, 14)
        direction = adx(highs, lows, closes, 14)
        adx14 = float(direction["ADX"] or 0)
        plus_di = float(direction["plus_DI"] or 0)
        minus_di = float(direction["minus_DI"] or 0)
        obv_values = obv(closes, volumes)
        obv_trend = (obv_values[-1] - obv_values[-21]) / max(sum(volumes[-20:]), 1)
        volume_ratio = volumes[-1] / max(statistics.fmean(volumes[-20:]), 1e-12)
        middle = statistics.fmean(closes[-20:])
        deviation = statistics.stdev(closes[-20:])
        upper = middle + 2 * deviation
        lower = middle - 2 * deviation

        trend_score = (
            100.0
            if closes[-1] > ema12 > ema26 and plus_di > minus_di
            else (60.0 if closes[-1] > ema26 else 20.0)
        )
        momentum_score = 100.0 if 50 <= rsi14 <= 70 else (60.0 if 40 <= rsi14 <= 80 else 20.0)
        band_score = (
            80.0
            if middle <= closes[-1] <= upper
            else (55.0 if lower <= closes[-1] < middle else 25.0)
        )
        volume_score = (60.0 if obv_trend > 0 else 20.0) + (40.0 if volume_ratio >= 1 else 20.0)
        score = statistics.fmean((trend_score, momentum_score, band_score, volume_score))
        signal = (
            SignalKind.BULLISH
            if score >= 70
            else (SignalKind.BEARISH if score < 40 else SignalKind.NEUTRAL)
        )

        observations: list[Observation] = []
        values = (
            ("technical_basic_score", score, "ohlcv", "composite_ema_adx_rsi_bollinger_obv"),
            ("exponential_moving_average_12", ema12, "close", "ema_12"),
            ("exponential_moving_average_26", ema26, "close", "ema_26"),
            ("relative_strength_index_14", rsi14, "close", "rsi_14"),
            ("adx_14", adx14, "ohlcv", "adx_14"),
            ("bollinger_middle_20", middle, "close", "bollinger_20_2"),
            ("bollinger_upper_20_2", upper, "close", "bollinger_20_2"),
            ("bollinger_lower_20_2", lower, "close", "bollinger_20_2"),
            ("on_balance_volume_trend_20", obv_trend, "ohlcv", "obv_20"),
            ("volume_ratio_20", volume_ratio, "ohlcv", "volume_ratio_20"),
        )
        for metric, value, input_metric, lookback in values:
            _append(
                observations,
                instrument,
                metric,
                value,
                prices,
                input_metric=input_metric,
                lookback=lookback,
            )

        limitations = (LimitationKind.INVALID_ROWS_DISCARDED,) if discarded else ()
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=sanitize_text(
                f"Technical score {score:.2f}/100; EMA trend, ADX, RSI, "
                "Bollinger bands, OBV, and volume were evaluated."
            ),
            status=ReportStatus.PARTIAL if discarded else ReportStatus.COMPLETE,
            limitations=limitations,
            methods=(
                AnalysisMethod(
                    algorithm=DerivedAlgorithm.TECHNICAL_BASIC_COMPOSITE,
                    window=f"{len(prices)}_daily_bars",
                ),
            ),
            signal=signal,
            observations=tuple(observations),
        )


@dataclass(frozen=True, slots=True)
class RiskAnalysisSkill:
    """Historical volatility, tail loss, drawdown, and return-shape statistics."""

    _name: str = field(default="risk-analysis", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.PRICES,)

    def __getattribute__(self, attribute: str) -> object:
        if attribute == "name":
            return object.__getattribute__(self, "_name")
        return object.__getattribute__(self, attribute)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        prices, discarded, _ = validated_prices(providers.prices(instrument))
        closes = [point.close for point in prices]
        returns = _simple_returns(closes)
        if len(returns) < _MIN_RISK_RETURNS:
            return _partial(
                instrument,
                self.name,
                f"Risk analysis requires at least {_MIN_RISK_RETURNS + 1} daily closes.",
                LimitationKind.INSUFFICIENT_HISTORY,
            )

        annualization = _annualization_days(instrument)
        annualized_vol = statistics.stdev(returns) * math.sqrt(annualization)
        downside_deviation = math.sqrt(
            statistics.fmean(min(value, 0.0) ** 2 for value in returns)
        )
        downside_vol = downside_deviation * math.sqrt(annualization)
        lower_tail = _quantile(returns, 0.05)
        var95 = max(0.0, -lower_tail)
        tail = [value for value in returns if value <= lower_tail]
        cvar95 = max(0.0, -statistics.fmean(tail))
        skewness, excess_kurtosis = _moments(returns)
        drawdown = max_drawdown(closes, len(closes)) or 0.0

        observations: list[Observation] = []
        values = (
            ("annualized_volatility", annualized_vol),
            ("downside_volatility", downside_vol),
            ("max_drawdown", drawdown),
            ("historical_var_95", var95),
            ("historical_cvar_95", cvar95),
            ("return_skewness", skewness),
            ("return_excess_kurtosis", excess_kurtosis),
            ("best_daily_return", max(returns)),
            ("worst_daily_return", min(returns)),
        )
        lookback = f"{len(returns)}_returns_{annualization}d"
        for metric, value in values:
            _append(observations, instrument, metric, value, prices, lookback=lookback)

        limitations = (LimitationKind.INVALID_ROWS_DISCARDED,) if discarded else ()
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=sanitize_text(
                f"Historical risk: annualized volatility {annualized_vol:.2%}, "
                f"max drawdown {drawdown:.2%}, VaR(95%) {var95:.2%}."
            ),
            status=ReportStatus.PARTIAL if discarded else ReportStatus.COMPLETE,
            limitations=limitations,
            methods=(
                AnalysisMethod(
                    algorithm=DerivedAlgorithm.HISTORICAL_RISK_STATISTICS,
                    window=lookback,
                ),
            ),
            signal=SignalKind.NOT_ASSESSED,
            observations=tuple(observations),
        )


@dataclass(frozen=True, slots=True)
class VolatilityRegimeSkill:
    """Classify realized volatility as compressed, normal, or elevated."""

    _name: str = field(default="volatility-regime", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.PRICES,)

    def __getattribute__(self, attribute: str) -> object:
        if attribute == "name":
            return object.__getattribute__(self, "_name")
        return object.__getattribute__(self, attribute)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        prices, discarded, _ = validated_prices(providers.prices(instrument))
        closes = [point.close for point in prices]
        returns = _log_returns(closes)
        required = _VOLATILITY_WINDOW + _REGIME_LOOKBACK
        if len(returns) < required:
            return _partial(
                instrument,
                self.name,
                f"Volatility regime requires at least {required + 1} daily closes.",
                LimitationKind.INSUFFICIENT_HISTORY,
            )

        annualization = _annualization_days(instrument)
        volatility = [
            statistics.stdev(returns[index - _VOLATILITY_WINDOW : index]) * math.sqrt(annualization)
            for index in range(_VOLATILITY_WINDOW, len(returns) + 1)
        ]
        distribution = volatility[-_REGIME_LOOKBACK:]
        current = distribution[-1]
        below = sum(value < current for value in distribution)
        tied = sum(value == current for value in distribution)
        percentile = (below + (tied - 1) / 2) / (len(distribution) - 1)
        code = 0.0 if percentile <= 0.2 else (2.0 if percentile >= 0.8 else 1.0)
        previous = distribution[-2]
        trend = 1.0 if current > previous * 1.05 else (-1.0 if current < previous * 0.95 else 0.0)
        regime = ("compressed", "normal", "elevated")[int(code)]
        trend_label = {-1.0: "contracting", 0.0: "stable", 1.0: "expanding"}[trend]

        observations: list[Observation] = []
        lookback = f"hv_{_VOLATILITY_WINDOW}_{_REGIME_LOOKBACK}_{annualization}d"
        for metric, value in (
            ("annualized_volatility", current),
            ("volatility_regime_percentile", percentile),
            ("volatility_regime_code", code),
            ("volatility_trend", trend),
        ):
            _append(observations, instrument, metric, value, prices, lookback=lookback)

        limitations = (LimitationKind.INVALID_ROWS_DISCARDED,) if discarded else ()
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=sanitize_text(
                f"Volatility regime: {regime}; {current:.2%} annualized, "
                f"{percentile:.2%} historical percentile, {trend_label}."
            ),
            status=ReportStatus.PARTIAL if discarded else ReportStatus.COMPLETE,
            limitations=limitations,
            methods=(
                AnalysisMethod(
                    algorithm=DerivedAlgorithm.VOLATILITY_REGIME_PERCENTILE,
                    window=lookback,
                ),
            ),
            signal=SignalKind.NOT_ASSESSED,
            observations=tuple(observations),
        )
