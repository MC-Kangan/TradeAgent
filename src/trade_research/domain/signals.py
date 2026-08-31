"""Canonical, source-independent trading signal events."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

SignalAction = Literal[
    "add_long",
    "add_short",
    "reduce_long",
    "exit_long",
]
SignalDirection = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """One deterministic signal emitted without knowledge of future outcomes."""

    observed_at: datetime
    action: SignalAction
    initial_risk: float | None = None
    indicator_values: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("signal timestamps must include a timezone")
        if self.action not in {
            "add_long",
            "add_short",
            "reduce_long",
            "exit_long",
        }:
            raise ValueError("unknown signal action")
        if self.initial_risk is not None and (
            not math.isfinite(self.initial_risk) or self.initial_risk <= 0
        ):
            raise ValueError("initial_risk must be positive and finite")
        if any(not math.isfinite(value) for _, value in self.indicator_values):
            raise ValueError("signal indicator values must be finite")

    @property
    def direction(self) -> SignalDirection:
        return "long" if self.action.endswith("_long") else "short"

    @property
    def is_entry(self) -> bool:
        return self.action.startswith("add_")
