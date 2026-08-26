"""Causal signal generators that are independent of data providers and evaluators."""

from __future__ import annotations

import math
from collections.abc import Sequence

from trade_research.providers import PricePoint
from trade_research.skills.signal_evaluation import SignalInstruction


def moving_average_crossover(
    prices: Sequence[PricePoint],
    *,
    fast_window: int = 20,
    slow_window: int = 50,
) -> tuple[SignalInstruction, ...]:
    """Return long/short instructions when the close SMA relationship changes.

    Each instruction is timestamped at the close that confirms the crossover.
    The evaluator's default one-bar entry lag therefore enters on the next bar.
    """

    if not 0 < fast_window < slow_window:
        raise ValueError("moving-average windows must satisfy 0 < fast_window < slow_window")
    if len(prices) < slow_window:
        return ()

    timestamps = [point.observed_at for point in prices]
    if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
        raise ValueError("prices must have ordered unique timestamps")
    closes = [point.close for point in prices]
    if any(not math.isfinite(value) or value <= 0 for value in closes):
        raise ValueError("prices must contain finite positive closes")

    fast_sum = math.fsum(closes[slow_window - fast_window : slow_window])
    slow_sum = math.fsum(closes[:slow_window])
    previous_relation = _relation(fast_sum / fast_window, slow_sum / slow_window)
    instructions: list[SignalInstruction] = []

    for index in range(slow_window, len(prices)):
        fast_sum += closes[index] - closes[index - fast_window]
        slow_sum += closes[index] - closes[index - slow_window]
        relation = _relation(fast_sum / fast_window, slow_sum / slow_window)
        if relation > 0 and previous_relation <= 0:
            instructions.append(
                SignalInstruction(observed_at=prices[index].observed_at, direction="long")
            )
        elif relation < 0 and previous_relation >= 0:
            instructions.append(
                SignalInstruction(observed_at=prices[index].observed_at, direction="short")
            )
        previous_relation = relation

    return tuple(instructions)


def _relation(left: float, right: float) -> int:
    if left > right:
        return 1
    if left < right:
        return -1
    return 0
