"""Pure, immutable technical-indicator primitives shared across price-based skills.

This module contains indicator calculations, price validation, provenance
builders, and result helpers extracted from ``core.py`` so they can be
reused by any skill that operates on OHLCV price data.  New indicator
functions needed by the worth-buy-stocks scoring pipeline are also included.

All functions are public.  Existing code in ``core.py`` imports them under
private aliases (e.g. ``ema_series as _ema_series``) so there is zero
behavioural change.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import cast

from trade_research.domain import (
    InstrumentId,
    LimitationKind,
    MetricKind,
    Observation,
)
from trade_research.domain.provenance import (
    MAX_PROVENANCE_ITEMS,
    normalize_provider_kind,
    sanitize_provider_reference,
)
from trade_research.providers import PricePoint

# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def is_finite(value: object) -> bool:
    """Return True when *value* is a finite int or float (not bool)."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def numeric_value(observation: Observation) -> float:
    """Extract a finite float from an Observation's value field."""
    v = observation.value
    if not is_finite(v):
        raise TypeError("factor input must be a finite number")
    return float(cast(int | float, v))


# ---------------------------------------------------------------------------
# Text & result helpers
# ---------------------------------------------------------------------------


def sanitize_text(text: str) -> str:
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


def summary(missing: Sequence[str]) -> str:
    """Build a standard AnalystResult summary from a missing-metric list."""
    if missing:
        return sanitize_text(
            f"partial data: missing {', '.join(dict.fromkeys(missing))}"
        )
    return "complete data: all required inputs available"


def missing_metric_kinds(missing: Sequence[str]) -> tuple[MetricKind, ...]:
    """Map free-form missing-item strings to closed-enum MetricKind values."""
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


def limitations(missing: Sequence[str]) -> tuple[LimitationKind, ...]:
    """Infer LimitationKind values from a free-form missing-metric list."""
    result: list[LimitationKind] = []
    joined = " ".join(missing)
    if missing:
        result.append(LimitationKind.MISSING_INPUTS)
    if "incompatible" in joined:
        result.append(LimitationKind.INCOMPATIBLE_INPUTS)
    if "discarded" in joined:
        result.append(LimitationKind.INVALID_ROWS_DISCARDED)
    if "OHLCV" in joined:
        result.append(LimitationKind.INCOMPLETE_OHLCV)
    if any(
        word in joined
        for word in ("moving_average", "relative_strength", "macd", "momentum", "volatility")
    ):
        result.append(LimitationKind.INSUFFICIENT_HISTORY)
    return tuple(dict.fromkeys(result))


# ---------------------------------------------------------------------------
# Price validation
# ---------------------------------------------------------------------------


def valid_price(point: PricePoint) -> bool:
    """Return False when *point* violates any OHLCV sanity constraint."""
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


def has_complete_ohlcv(point: PricePoint) -> bool:
    """Return True when all five OHLCV fields are non-None."""
    return all(value is not None for value in (point.open, point.high, point.low, point.volume))


def validated_prices(
    supplied: Sequence[PricePoint],
) -> tuple[tuple[PricePoint, ...], bool, bool]:
    """Deduplicate, validate, sort, and flag issues in a price series.

    Returns ``(clean, discarded, incomplete_ohlcv)``.
    """
    timestamp_counts = Counter(point.observed_at for point in supplied)
    valid: list[PricePoint] = []
    discarded = False
    incomplete_ohlcv = False
    for point in supplied:
        if timestamp_counts[point.observed_at] > 1 or not valid_price(point):
            discarded = True
            continue
        if not has_complete_ohlcv(point):
            incomplete_ohlcv = True
        valid.append(point)
    return tuple(sorted(valid, key=lambda point: point.observed_at)), discarded, incomplete_ohlcv


# ---------------------------------------------------------------------------
# Provenance builders
# ---------------------------------------------------------------------------


def algorithm_for_metric(metric: str) -> str:
    """Map a metric name to its canonical DerivedAlgorithm label."""
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
    # Worth-buy-stocks extensions
    if metric.startswith("kdj_"):
        return "kdj_calculation"
    if metric.startswith("adx_"):
        return "adx_calculation"
    if metric.startswith("efficiency_ratio"):
        return "efficiency_ratio"
    if metric.startswith("worth_buy_"):
        return "worth_buy_alpha_weighted"
    # Markov regime detection extensions
    if metric.startswith("markov_current_regime") or metric.startswith("markov_signal"):
        return "markov_regime_detection"
    if metric.startswith("markov_stationary_"):
        return "markov_stationary_distribution"
    if metric.startswith("markov_persistence_"):
        return "markov_transition_matrix"
    if metric.startswith("markov_walkforward_"):
        return "markov_walkforward"
    if metric.startswith("technical_basic_"):
        return "technical_basic_composite"
    if metric.startswith("on_balance_volume_") or metric.startswith("volume_ratio_"):
        return "technical_basic_composite"
    if metric in {
        "annualized_volatility",
        "downside_volatility",
        "historical_var_95",
        "historical_cvar_95",
        "return_skewness",
        "return_excess_kurtosis",
        "best_daily_return",
        "worst_daily_return",
        "max_drawdown",
    }:
        return "historical_risk_statistics"
    if metric.startswith("volatility_regime_") or metric == "volatility_trend":
        return "volatility_regime_percentile"
    raise ValueError(f"unknown derived price metric '{metric}'")


def price_series_reference(prices: Sequence[PricePoint], metric: str) -> str:
    """SHA-256 hash of a canonical representation of the price series."""
    values = [price_input(point, metric) for point in prices]
    canonical = json.dumps(values, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def price_input(point: PricePoint, metric: str) -> dict[str, object]:
    """Build a provenance input dict for a single PricePoint."""
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


def factor(
    instrument: InstrumentId,
    metric: str,
    value: float,
    inputs: tuple[Observation, ...],
    *,
    algorithm: str,
    window: str,
) -> Observation:
    """Build a single derived Observation with full input provenance."""
    return Observation(
        instrument=instrument,
        metric=metric,
        value=round(value, 10),
        source="derived",
        observed_at=max(item.observed_at for item in inputs),
        provenance={
            "algorithm": algorithm,
            "window": window,
            "inputs": [observation_input(item) for item in inputs],
        },
    )


def observation_input(observation: Observation) -> dict[str, object]:
    """Extract provenance metadata from an Observation for use as an input ref."""
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


def append_price_factor(
    factors: list[Observation],
    instrument: InstrumentId,
    metric: str,
    value: float,
    prices: Sequence[PricePoint],
    input_metric: str,
    lookback: str,
) -> None:
    """Append a price-derived Observation with canonical provenance."""
    provenance: dict[str, object] = {
        "algorithm": algorithm_for_metric(metric),
        "window": lookback,
        "point_count": len(prices),
        "start_at": min(point.observed_at for point in prices).isoformat(),
        "end_at": max(point.observed_at for point in prices).isoformat(),
        "series_ref": price_series_reference(prices, input_metric),
        "input_provider_kind": normalize_provider_kind(prices[0].source).value,
    }
    if len(prices) <= MAX_PROVENANCE_ITEMS:
        provenance["inputs"] = [price_input(point, input_metric) for point in prices]
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


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """EMA series seeded with the SMA of the first *period* values."""
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = statistics.fmean(values[:period])
    result = [ema]
    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
        result.append(ema)
    return result


def rsi(values: Sequence[float], period: int) -> float:
    """Wilder's RSI(period).  Returns 50.0 for a flat series."""
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    average_gain = statistics.fmean(max(change, 0) for change in changes[:period])
    average_loss = statistics.fmean(max(-change, 0) for change in changes[:period])
    for change in changes[period:]:
        average_gain = (average_gain * (period - 1) + max(change, 0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0)) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100 - 100 / (1 + average_gain / average_loss)


def macd_series(values: Sequence[float], fast: int, slow: int) -> list[float]:
    """MACD line = fast EMA minus slow EMA at each overlapping index."""
    if len(values) < slow:
        return []
    fast_values = ema_series(values, fast)
    slow_values = ema_series(values, slow)
    fast_offset = slow - fast
    return [fast_values[index + fast_offset] - s for index, s in enumerate(slow_values)]


def atr(prices: Sequence[PricePoint], period: int) -> float:
    """Wilder's ATR(period) from complete-OHLCV PricePoints."""
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


def sma(values: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last *period* values.  None if insufficient."""
    if len(values) < period:
        return None
    return statistics.fmean(values[-period:])


# ---------------------------------------------------------------------------
# New indicators for worth-buy-stocks scoring pipeline
# ---------------------------------------------------------------------------


def kdj(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 9,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> dict[str, float | None]:
    """KDJ stochastic oscillator.

    Returns ``{"K": float|None, "D": float|None, "J": float|None}``.
    %K = SMA(RSV, k_smooth),  %D = SMA(%K, d_smooth),  %J = 3*%K - 2*%D.
    """
    if len(closes) < period:
        return {"K": None, "D": None, "J": None}

    k_values: list[float] = []
    d_values: list[float] = []
    j_values: list[float] = []

    for i in range(period - 1, len(closes)):
        window_high = max(highs[i - period + 1 : i + 1])
        window_low = min(lows[i - period + 1 : i + 1])
        if window_high == window_low:
            rsv = 50.0
        else:
            rsv = (closes[i] - window_low) / (window_high - window_low) * 100.0
        k_values.append(rsv)

    # Smooth K with SMA of k_smooth
    if len(k_values) >= k_smooth:
        smoothed_k: list[float] = []
        for i in range(len(k_values)):
            start = max(0, i - k_smooth + 1)
            smoothed_k.append(statistics.fmean(k_values[start : i + 1]))
        k_values = smoothed_k

    # Smooth D with SMA of d_smooth over K
    if len(k_values) >= d_smooth:
        for i in range(len(k_values)):
            start = max(0, i - d_smooth + 1)
            d_values.append(statistics.fmean(k_values[start : i + 1]))

    # J = 3*K - 2*D (aligned to latest values)
    min_len = min(len(k_values), len(d_values))
    for i in range(min_len):
        j_values.append(3 * k_values[i] - 2 * d_values[i])

    return {
        "K": k_values[-1] if k_values else None,
        "D": d_values[-1] if d_values else None,
        "J": j_values[-1] if j_values else None,
    }


def adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> dict[str, float | None]:
    """Average Directional Index (ADX) with +DI and -DI.

    Returns ``{"ADX": float|None, "plus_DI": float|None, "minus_DI": float|None}``.
    """
    if len(closes) < 2 * period + 1:
        return {"ADX": None, "plus_DI": None, "minus_DI": None}

    tr_values: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []

    for i in range(1, len(closes)):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        prev_high = highs[i - 1]
        prev_low = lows[i - 1]

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)

        up_move = high - prev_high
        down_move = prev_low - low

        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0.0)

        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0.0)

    # Wilder smoothing
    atr_val = statistics.fmean(tr_values[:period])
    plus_di_val = statistics.fmean(plus_dm[:period])
    minus_di_val = statistics.fmean(minus_dm[:period])

    final_pdi: float | None = None
    final_mdi: float | None = None
    dx_values: list[float] = []
    for i in range(period, len(tr_values)):
        atr_val = (atr_val * (period - 1) + tr_values[i]) / period
        plus_di_val = (plus_di_val * (period - 1) + plus_dm[i]) / period
        minus_di_val = (minus_di_val * (period - 1) + minus_dm[i]) / period
        if atr_val == 0:
            continue
        final_pdi = (plus_di_val / atr_val) * 100
        final_mdi = (minus_di_val / atr_val) * 100
        if final_pdi + final_mdi == 0:
            continue
        dx_values.append(abs(final_pdi - final_mdi) / (final_pdi + final_mdi) * 100)

    if not dx_values:
        return {"ADX": None, "plus_DI": final_pdi, "minus_DI": final_mdi}

    # Smooth DX to get ADX
    adx_val = statistics.fmean(dx_values[:period])
    for i in range(period, len(dx_values)):
        adx_val = (adx_val * (period - 1) + dx_values[i]) / period

    return {
        "ADX": adx_val,
        "plus_DI": final_pdi,
        "minus_DI": final_mdi,
    }


def obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    """On-Balance Volume cumulative series."""
    if len(closes) < 2 or len(volumes) != len(closes):
        return []
    result = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            result.append(result[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            result.append(result[-1] - volumes[i])
        else:
            result.append(result[-1])
    return result


def efficiency_ratio(closes: Sequence[float], period: int = 30) -> float | None:
    """Kaufman efficiency ratio: net change / sum of absolute moves.  ∈ [0, 1]."""
    if len(closes) < period + 1:
        return None
    window = closes[-period - 1 :]
    net_change = abs(window[-1] - window[0])
    path_length = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    if path_length == 0:
        return 1.0
    return net_change / path_length


def max_drawdown(closes: Sequence[float], window: int = 252) -> float | None:
    """Peak-to-trough maximum drawdown over *window* bars, as negative fraction."""
    if len(closes) < 2:
        return None
    w = closes[-window:] if len(closes) >= window else closes
    peak = w[0]
    worst = 0.0
    for price in w[1:]:
        if price > peak:
            peak = price
        dd = (price - peak) / peak
        if dd < worst:
            worst = dd
    return worst


def annualized_volatility(closes: Sequence[float], window: int = 63) -> float | None:
    """Annualized volatility from daily log returns over *window* bars."""
    if len(closes) < window + 1:
        return None
    w = closes[-window - 1 :]
    returns = [math.log(w[i] / w[i - 1]) for i in range(1, len(w))]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(252)


def up_down_volume_ratio(
    closes: Sequence[float], volumes: Sequence[float], window: int = 10
) -> float | None:
    """Ratio of average up-day volume to average down-day volume over *window*."""
    if len(closes) < window + 1 or len(volumes) != len(closes):
        return None
    w_closes = closes[-window - 1 :]
    w_vols = volumes[-window - 1 :]
    up_vols: list[float] = []
    down_vols: list[float] = []
    for i in range(1, len(w_closes)):
        if w_closes[i] > w_closes[i - 1]:
            up_vols.append(w_vols[i])
        elif w_closes[i] < w_closes[i - 1]:
            down_vols.append(w_vols[i])
    if not down_vols:
        return 2.0 if up_vols else 1.0
    if not up_vols:
        return 0.5 if down_vols else 1.0
    avg_up = statistics.fmean(up_vols)
    avg_down = statistics.fmean(down_vols)
    if avg_down == 0:
        return 2.0
    return avg_up / avg_down


def momentum_12_1(closes: Sequence[float]) -> float | None:
    """Return from t-252 to t-21, skipping the most recent month (avoids reversal)."""
    if len(closes) < 253:
        return None
    return closes[-22] / closes[-253] - 1


def to_weekly(bars: Sequence[PricePoint]) -> list[dict[str, object]]:
    """Aggregate daily bars to ISO-week OHLCV candles."""
    if not bars:
        return []
    weeks: dict[str, dict[str, object]] = {}
    for bar in bars:
        if bar.observed_at.tzinfo is None:
            continue
        # ISO week tuple: (year, week_number, weekday)
        iso = bar.observed_at.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        if key not in weeks:
            weeks[key] = {
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "observed_at": bar.observed_at,
                "count": 1,
            }
        else:
            w = weeks[key]
            # open stays as first bar's open (already set)
            if bar.high is not None and (
                w["high"] is None or bar.high > cast(float, w["high"])
            ):
                w["high"] = bar.high
            if bar.low is not None and (
                w["low"] is None or bar.low < cast(float, w["low"])
            ):
                w["low"] = bar.low
            w["close"] = bar.close  # last bar's close
            if bar.volume is not None:
                w["volume"] = cast(float, w["volume"] or 0) + bar.volume
            w["observed_at"] = bar.observed_at  # last bar's timestamp
            w["count"] = cast(int, w["count"]) + 1

    return sorted(weeks.values(), key=lambda w: cast(datetime, w["observed_at"]))


def weekly_bearish_check(weekly_closes: list[float]) -> dict[str, bool | None | float]:
    """Check if weekly MAs are in bearish alignment (MA5 < MA10 < MA20 < MA30)."""
    if len(weekly_closes) < 30:
        return {"bearish": None, "alignment_strength": None}

    ma5 = sma(weekly_closes, 5)
    ma10 = sma(weekly_closes, 10)
    ma20 = sma(weekly_closes, 20)
    ma30 = sma(weekly_closes, 30)

    if any(m is None for m in (ma5, ma10, ma20, ma30)):
        return {"bearish": None, "alignment_strength": None}

    ma5_v = cast(float, ma5)
    ma10_v = cast(float, ma10)
    ma20_v = cast(float, ma20)
    ma30_v = cast(float, ma30)

    bearish = ma5_v < ma10_v < ma20_v < ma30_v
    # Alignment strength: spread between MA5 and MA30 as % of MA30
    strength = (ma30_v - ma5_v) / ma30_v if ma30_v != 0 else 0.0
    # Require at least 1% spread to avoid false triggers in sideways markets
    significant = strength >= 0.01

    result: dict[str, bool | None | float] = {
        "bearish": bearish and significant,
        "alignment_strength": round(strength, 4),
    }
    return result
