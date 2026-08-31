"""Causal signal proxies for the ten systems in the supplied X article.

The generators produce entry instructions only.  Position sizing and bespoke
exits remain strategy metadata so a common outcome evaluator can compare entry
signal quality without mixing incompatible execution models.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trade_research.domain import SignalAction, SignalDirection, SignalEvent
from trade_research.providers import PricePoint

StrategyCategory = Literal[
    "daily-trend",
    "daily-mean-reversion",
    "intraday-trend",
    "intraday-hybrid",
    "position-sizing-overlay",
]


@dataclass(frozen=True, slots=True)
class ClassicStrategyDefinition:
    """Identity and evaluation posture for one article-listed system."""

    number: int
    slug: str
    name: str
    category: StrategyCategory
    timeframe: Literal["daily", "intraday", "not-applicable"]
    signal_evaluable: bool = True


CLASSIC_STRATEGIES = (
    ClassicStrategyDefinition(1, "turtle", "Turtle Trading", "daily-trend", "daily"),
    ClassicStrategyDefinition(2, "gap", "Gap Trading", "daily-mean-reversion", "daily"),
    ClassicStrategyDefinition(3, "dolphin", "Dolphin Trading", "intraday-trend", "intraday"),
    ClassicStrategyDefinition(
        4,
        "martingale",
        "Martingale",
        "position-sizing-overlay",
        "not-applicable",
        signal_evaluable=False,
    ),
    ClassicStrategyDefinition(5, "r-breaker", "R-Breaker", "intraday-hybrid", "intraday"),
    ClassicStrategyDefinition(6, "dual-thrust", "Dual Thrust", "intraday-trend", "intraday"),
    ClassicStrategyDefinition(
        7, "fairy-four-price", "Fairy Four Price", "intraday-trend", "intraday"
    ),
    ClassicStrategyDefinition(
        8, "oliver-kell-ema", "Oliver-Kell EMA", "daily-trend", "daily"
    ),
    ClassicStrategyDefinition(9, "escalator", "Escalator", "daily-trend", "daily"),
    ClassicStrategyDefinition(10, "checkmate", "Checkmate", "daily-trend", "daily"),
)


def turtle_signals(
    prices: Sequence[PricePoint],
    *,
    entry_window: int = 20,
    exit_window: int = 10,
) -> tuple[SignalEvent, ...]:
    """Article Turtle proxy: prior-channel entries with opposite-channel exits."""

    clean = _complete_prices(prices)
    if not 0 < exit_window < entry_window:
        raise ValueError("Turtle windows must satisfy 0 < exit_window < entry_window")
    position = 0
    result: list[SignalEvent] = []
    for index in range(entry_window, len(clean)):
        point = clean[index]
        prior_entry = clean[index - entry_window : index]
        prior_exit = clean[index - exit_window : index]
        if position > 0 and point.close < min(_low(item) for item in prior_exit):
            position = 0
        elif position < 0 and point.close > max(_high(item) for item in prior_exit):
            position = 0
        if position == 0 and point.close > max(_high(item) for item in prior_entry):
            result.append(_instruction(point, "long"))
            position = 1
        elif position == 0 and point.close < min(_low(item) for item in prior_entry):
            result.append(_instruction(point, "short"))
            position = -1
    return tuple(result)


def gap_signals(
    prices: Sequence[PricePoint],
    *,
    atr_period: int = 14,
    gap_atr: float = 0.5,
) -> tuple[SignalEvent, ...]:
    """Contrarian opening-gap signals using only the prior session ATR."""

    clean = _complete_prices(prices)
    if atr_period < 1 or gap_atr <= 0:
        raise ValueError("gap parameters must be positive")
    atr_values = _atr_series(clean, atr_period)
    result: list[SignalEvent] = []
    for index in range(1, len(clean)):
        prior_atr = atr_values[index - 1]
        if prior_atr is None:
            continue
        point = clean[index]
        previous = clean[index - 1]
        if _open(point) < _low(previous) - gap_atr * prior_atr:
            result.append(_instruction(point, "long"))
        elif _open(point) > _high(previous) + gap_atr * prior_atr:
            result.append(_instruction(point, "short"))
    return tuple(result)


def dolphin_signals(
    prices: Sequence[PricePoint],
    *,
    fast_window: int = 5,
    slow_window: int = 20,
    breakout_bars: int = 2,
) -> tuple[SignalEvent, ...]:
    """Article Dolphin proxy: EMA trend plus a prior-bar momentum breakout."""

    clean = _complete_prices(prices)
    if not 0 < fast_window < slow_window or breakout_bars < 1:
        raise ValueError("Dolphin windows are invalid")
    closes = [point.close for point in clean]
    fast = _ema_aligned(closes, fast_window)
    slow = _ema_aligned(closes, slow_window)
    result: list[SignalEvent] = []
    previous_long = False
    previous_short = False
    start = max(slow_window - 1, breakout_bars)
    for index in range(start, len(clean)):
        fast_value = fast[index]
        slow_value = slow[index]
        if fast_value is None or slow_value is None:
            continue
        prior = clean[index - breakout_bars : index]
        long_condition = fast_value > slow_value and clean[index].close > max(
            _high(point) for point in prior
        )
        short_condition = fast_value < slow_value and clean[index].close < min(
            _low(point) for point in prior
        )
        if long_condition and not previous_long:
            result.append(_instruction(clean[index], "long"))
        elif short_condition and not previous_short:
            result.append(_instruction(clean[index], "short"))
        previous_long = long_condition
        previous_short = short_condition
    return tuple(result)


def r_breaker_signals(
    prices: Sequence[PricePoint],
    *,
    breakout_fraction: float = 0.35,
    session_timezone: str = "America/New_York",
) -> tuple[SignalEvent, ...]:
    """Transparent proxy for the article's simplified R-Breaker description.

    Breakouts use pivot +/- ``breakout_fraction`` times the previous session
    range.  Reversals require an excursion through pivot +/- one full range and
    a recovery through the nearer breakout line.
    """

    if breakout_fraction <= 0:
        raise ValueError("R-Breaker breakout_fraction must be positive")
    sessions = _sessions(_complete_prices(prices), session_timezone)
    result: list[SignalEvent] = []
    for session_index in range(1, len(sessions)):
        previous = sessions[session_index - 1][1]
        current = sessions[session_index][1]
        high = max(_high(point) for point in previous)
        low = min(_low(point) for point in previous)
        close = previous[-1].close
        pivot = (high + low + close) / 3
        width = high - low
        upper = pivot + breakout_fraction * width
        lower = pivot - breakout_fraction * width
        deep_support = pivot - width
        high_resistance = pivot + width
        touched_support = False
        touched_resistance = False
        for point in current:
            touched_support = touched_support or _low(point) <= deep_support
            touched_resistance = touched_resistance or _high(point) >= high_resistance
            if touched_support and touched_resistance:
                continue
            if touched_support:
                if point.close > lower:
                    result.append(_instruction(point, "long"))
                    break
                continue
            if touched_resistance:
                if point.close < upper:
                    result.append(_instruction(point, "short"))
                    break
                continue
            if point.close >= upper:
                result.append(_instruction(point, "long"))
                break
            if point.close <= lower:
                result.append(_instruction(point, "short"))
                break
    return tuple(result)


def dual_thrust_signals(
    prices: Sequence[PricePoint],
    *,
    coefficient: float = 0.5,
    session_timezone: str = "America/New_York",
) -> tuple[SignalEvent, ...]:
    """Article Dual Thrust proxy using the maximum of two prior daily ranges."""

    if coefficient <= 0:
        raise ValueError("Dual Thrust coefficient must be positive")
    sessions = _sessions(_complete_prices(prices), session_timezone)
    result: list[SignalEvent] = []
    for session_index in range(2, len(sessions)):
        first = sessions[session_index - 2][1]
        second = sessions[session_index - 1][1]
        current = sessions[session_index][1]
        range_value = max(
            max(_high(point) for point in first) - min(_low(point) for point in first),
            max(_high(point) for point in second) - min(_low(point) for point in second),
        )
        open_value = _open(current[0])
        upper = open_value + coefficient * range_value
        lower = open_value - coefficient * range_value
        for point in current:
            if point.close >= upper:
                result.append(_instruction(point, "long"))
                break
            if point.close <= lower:
                result.append(_instruction(point, "short"))
                break
    return tuple(result)


def fairy_four_price_signals(
    prices: Sequence[PricePoint],
    *,
    session_timezone: str = "America/New_York",
) -> tuple[SignalEvent, ...]:
    """First intraday close beyond the previous session high or low."""

    sessions = _sessions(_complete_prices(prices), session_timezone)
    result: list[SignalEvent] = []
    for session_index in range(1, len(sessions)):
        previous = sessions[session_index - 1][1]
        current = sessions[session_index][1]
        prior_high = max(_high(point) for point in previous)
        prior_low = min(_low(point) for point in previous)
        for point in current:
            if point.close > prior_high:
                result.append(_instruction(point, "long"))
                break
            if point.close < prior_low:
                result.append(_instruction(point, "short"))
                break
    return tuple(result)


def ema_momentum_signals(
    prices: Sequence[PricePoint],
    *,
    fast_window: int = 5,
    slow_window: int = 20,
) -> tuple[SignalEvent, ...]:
    """EMA crossover instructions matching the article's item eight."""

    clean = _complete_prices(prices)
    if not 0 < fast_window < slow_window:
        raise ValueError("EMA windows must satisfy 0 < fast_window < slow_window")
    closes = [point.close for point in clean]
    fast = _ema_aligned(closes, fast_window)
    slow = _ema_aligned(closes, slow_window)
    if len(clean) < slow_window:
        return ()
    previous = _relation(cast(float, fast[slow_window - 1]), cast(float, slow[slow_window - 1]))
    result: list[SignalEvent] = []
    for index in range(slow_window, len(clean)):
        relation = _relation(cast(float, fast[index]), cast(float, slow[index]))
        if relation > 0 and previous <= 0:
            result.append(_instruction(clean[index], "long"))
        elif relation < 0 and previous >= 0:
            result.append(_instruction(clean[index], "short"))
        previous = relation
    return tuple(result)


def escalator_signals(
    prices: Sequence[PricePoint],
    *,
    breakout_window: int = 20,
    atr_period: int = 14,
    stop_multiplier: float = 3.0,
) -> tuple[SignalEvent, ...]:
    """Article Escalator proxy with channel entries and a ratcheting ATR stop."""

    clean = _complete_prices(prices)
    if breakout_window < 1 or atr_period < 1 or stop_multiplier <= 0:
        raise ValueError("Escalator parameters must be positive")
    atr_values = _atr_series(clean, atr_period)
    position = 0
    extreme = 0.0
    result: list[SignalEvent] = []
    for index in range(breakout_window, len(clean)):
        point = clean[index]
        atr_value = atr_values[index]
        if position > 0 and atr_value is not None:
            extreme = max(extreme, _high(point))
            if point.close < extreme - stop_multiplier * atr_value:
                position = 0
        elif position < 0 and atr_value is not None:
            extreme = min(extreme, _low(point))
            if point.close > extreme + stop_multiplier * atr_value:
                position = 0
        prior = clean[index - breakout_window : index]
        if position == 0 and point.close > max(_high(item) for item in prior):
            result.append(_instruction(point, "long"))
            position = 1
            extreme = _high(point)
        elif position == 0 and point.close < min(_low(item) for item in prior):
            result.append(_instruction(point, "short"))
            position = -1
            extreme = _low(point)
    return tuple(result)


def checkmate_signals(
    prices: Sequence[PricePoint],
    *,
    breakout_window: int = 40,
    fast_atr_period: int = 20,
    slow_atr_period: int = 60,
    expansion_ratio: float = 1.1,
) -> tuple[SignalEvent, ...]:
    """Article Checkmate proxy: channel breakout gated by ATR expansion."""

    clean = _complete_prices(prices)
    if not 0 < fast_atr_period < slow_atr_period or breakout_window < 1:
        raise ValueError("Checkmate windows are invalid")
    if expansion_ratio <= 0:
        raise ValueError("Checkmate expansion_ratio must be positive")
    fast_atr = _atr_series(clean, fast_atr_period)
    slow_atr = _atr_series(clean, slow_atr_period)
    start = max(breakout_window, slow_atr_period - 1)
    position = 0
    result: list[SignalEvent] = []
    for index in range(start, len(clean)):
        fast = fast_atr[index]
        slow = slow_atr[index]
        if fast is None or slow is None or fast <= expansion_ratio * slow:
            continue
        prior = clean[index - breakout_window : index]
        if clean[index].close > max(_high(point) for point in prior) and position <= 0:
            result.append(_instruction(clean[index], "long"))
            position = 1
        elif clean[index].close < min(_low(point) for point in prior) and position >= 0:
            result.append(_instruction(clean[index], "short"))
            position = -1
    return tuple(result)


def _complete_prices(prices: Sequence[PricePoint]) -> tuple[PricePoint, ...]:
    clean = tuple(prices)
    timestamps = [point.observed_at for point in clean]
    if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
        raise ValueError("prices must have ordered unique timestamps")
    for point in clean:
        values = (point.open, point.high, point.low, point.close)
        if any(value is None or not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("classic strategies require complete positive OHLC bars")
        if _low(point) > min(_open(point), point.close) or _high(point) < max(
            _open(point), point.close
        ):
            raise ValueError("classic strategies require internally consistent OHLC bars")
    return clean


def _atr_series(prices: Sequence[PricePoint], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("ATR period must be positive")
    true_ranges: list[float] = []
    for index, point in enumerate(prices):
        if index == 0:
            true_ranges.append(_high(point) - _low(point))
        else:
            previous_close = prices[index - 1].close
            true_ranges.append(
                max(
                    _high(point) - _low(point),
                    abs(_high(point) - previous_close),
                    abs(_low(point) - previous_close),
                )
            )
    result: list[float | None] = [None] * len(prices)
    if len(true_ranges) < period:
        return result
    average = statistics.fmean(true_ranges[:period])
    result[period - 1] = average
    for index in range(period, len(true_ranges)):
        average = (average * (period - 1) + true_ranges[index]) / period
        result[index] = average
    return result


def _ema_aligned(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    average = statistics.fmean(values[:period])
    result[period - 1] = average
    multiplier = 2 / (period + 1)
    for index in range(period, len(values)):
        average = (values[index] - average) * multiplier + average
        result[index] = average
    return result


def _sessions(
    prices: Sequence[PricePoint], timezone_name: str
) -> tuple[tuple[date, tuple[PricePoint, ...]], ...]:
    timezone = ZoneInfo(timezone_name)
    grouped: list[tuple[date, list[PricePoint]]] = []
    for point in prices:
        session_date = point.observed_at.astimezone(timezone).date()
        if not grouped or grouped[-1][0] != session_date:
            grouped.append((session_date, []))
        grouped[-1][1].append(point)
    return tuple((session_date, tuple(points)) for session_date, points in grouped)


def _instruction(point: PricePoint, direction: SignalDirection) -> SignalEvent:
    return SignalEvent(
        observed_at=point.observed_at,
        action=cast(SignalAction, f"add_{direction}"),
    )


def _open(point: PricePoint) -> float:
    return cast(float, point.open)


def _high(point: PricePoint) -> float:
    return cast(float, point.high)


def _low(point: PricePoint) -> float:
    return cast(float, point.low)


def _relation(left: float, right: float) -> int:
    if left > right:
        return 1
    if left < right:
        return -1
    return 0
