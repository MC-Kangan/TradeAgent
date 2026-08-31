"""Reusable composition helpers for the local strategy-evaluator showcase."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from trade_research.domain import SignalEvent
from trade_research.providers import OutcomePoint, PricePoint
from trade_research.signals import moving_average_crossover
from trade_research.skills.classic_strategy_signals import (
    checkmate_signals,
    dolphin_signals,
    dual_thrust_signals,
    ema_momentum_signals,
    escalator_signals,
    fairy_four_price_signals,
    gap_signals,
    r_breaker_signals,
    turtle_signals,
)
from trade_research.skills.signal_evaluation import (
    EvaluationPeriod,
    OutcomeSpecification,
    SignalEvaluationStudy,
    evaluate_signals,
)

Timeframe = Literal["daily", "intraday"]
type SignalGenerator = Callable[[Sequence[PricePoint]], tuple[SignalEvent, ...]]
type ChartValue = str | float | int | bool


@dataclass(frozen=True, slots=True)
class ShowcaseStrategy:
    """A signal generator plus the text needed to explain it in a UI."""

    slug: str
    name: str
    timeframe: Timeframe
    entry_criteria: str
    exit_criteria: str
    generator: SignalGenerator


@dataclass(frozen=True, slots=True)
class EvaluationSettings:
    """User-selectable outcome rules passed directly to the evaluator."""

    fixed_horizon_bars: int
    profit_target: float
    stop_loss: float
    max_holding_bars: int
    entry_lag_bars: int = 1
    bootstrap_samples: int = 500
    holdout_fraction: float = 0.2

    def __post_init__(self) -> None:
        if not 0 < self.holdout_fraction < 0.5:
            raise ValueError("holdout_fraction must be between zero and one half")


@dataclass(frozen=True, slots=True)
class StrategyEvaluationResult:
    """The selected strategy instructions and their evaluator output."""

    strategy: ShowcaseStrategy
    raw_instructions: tuple[SignalEvent, ...]
    instructions: tuple[SignalEvent, ...]
    study: SignalEvaluationStudy | None


_COMMON_EXIT = (
    "No native exit is encoded by this entry generator. The showcase exits at the "
    "selected profit target, stop loss, or maximum holding period."
)

_STRATEGIES = (
    ShowcaseStrategy(
        "turtle",
        "Turtle Trading",
        "daily",
        "Go long on a close above the prior 20-bar high, or short below the prior "
        "20-bar low, while the signal proxy is flat.",
        "The signal proxy resets after the opposite 10-bar channel breaks; evaluated "
        "trades use the showcase target, stop, and time limit.",
        turtle_signals,
    ),
    ShowcaseStrategy(
        "gap",
        "Gap Trading",
        "daily",
        "Go long when the open gaps below the prior low by more than 0.5 prior ATR, "
        "or short when it gaps above the prior high by that amount.",
        _COMMON_EXIT,
        gap_signals,
    ),
    ShowcaseStrategy(
        "dolphin",
        "Dolphin Trading",
        "intraday",
        "Enter with the 5/20 EMA trend when price also breaks the prior two-bar range; "
        "a signal is emitted when that condition becomes active.",
        _COMMON_EXIT,
        dolphin_signals,
    ),
    ShowcaseStrategy(
        "r-breaker",
        "R-Breaker",
        "intraday",
        "Use the prior session pivot and range to enter a breakout, or enter a reversal "
        "after a deep excursion recovers through the nearer trigger.",
        _COMMON_EXIT,
        r_breaker_signals,
    ),
    ShowcaseStrategy(
        "dual-thrust",
        "Dual Thrust",
        "intraday",
        "Enter on the first close beyond the current session open plus or minus half of "
        "the larger of the two previous session ranges.",
        _COMMON_EXIT,
        dual_thrust_signals,
    ),
    ShowcaseStrategy(
        "fairy-four-price",
        "Fairy Four Price",
        "intraday",
        "Enter on the first intraday close above the previous session high or below the "
        "previous session low.",
        _COMMON_EXIT,
        fairy_four_price_signals,
    ),
    ShowcaseStrategy(
        "oliver-kell-ema",
        "Oliver-Kell EMA",
        "daily",
        "Go long when the 5-bar EMA crosses above the 20-bar EMA, and short when it "
        "crosses below.",
        _COMMON_EXIT,
        ema_momentum_signals,
    ),
    ShowcaseStrategy(
        "escalator",
        "Escalator",
        "daily",
        "Enter on a close beyond the prior 20-bar channel while the signal proxy is flat.",
        "The signal proxy resets using a three-ATR ratcheting stop; evaluated trades use "
        "the showcase target, stop, and time limit.",
        escalator_signals,
    ),
    ShowcaseStrategy(
        "checkmate",
        "Checkmate",
        "daily",
        "Enter a 40-bar channel breakout only when 20-bar ATR exceeds 1.1 times 60-bar ATR.",
        _COMMON_EXIT,
        checkmate_signals,
    ),
    ShowcaseStrategy(
        "moving-average",
        "Moving-average crossover",
        "daily",
        "Go long when the 20-bar simple moving average crosses above the 50-bar average, "
        "and short when it crosses below.",
        _COMMON_EXIT,
        moving_average_crossover,
    ),
)
_STRATEGIES_BY_SLUG = {strategy.slug: strategy for strategy in _STRATEGIES}


def available_strategies() -> tuple[ShowcaseStrategy, ...]:
    """Return the fixed strategies that produce independently evaluable signals."""

    return _STRATEGIES


def evaluate_strategy(
    prices: Sequence[PricePoint],
    strategy_slug: str,
    settings: EvaluationSettings,
) -> StrategyEvaluationResult:
    """Generate instructions and pass them to the shared signal evaluator."""

    try:
        strategy = _STRATEGIES_BY_SLUG[strategy_slug]
    except KeyError as error:
        raise ValueError(f"unknown showcase strategy: {strategy_slug}") from error
    clean_prices = tuple(prices)
    if len(clean_prices) < 2:
        raise ValueError("at least two price bars are required")

    raw_instructions = strategy.generator(clean_prices)
    instructions = raw_instructions
    if strategy.timeframe == "intraday":
        instructions = _same_session_instructions(
            raw_instructions,
            clean_prices,
            required_forward_bars=max(
                settings.fixed_horizon_bars,
                settings.max_holding_bars,
            )
            + settings.entry_lag_bars,
        )
    if not instructions:
        return StrategyEvaluationResult(
            strategy=strategy,
            raw_instructions=raw_instructions,
            instructions=instructions,
            study=None,
        )

    split_index = max(1, int(len(clean_prices) * (1 - settings.holdout_fraction)))
    specification = OutcomeSpecification(
        change_kind="relative",
        fixed_horizon_bars=settings.fixed_horizon_bars,
        profit_target=settings.profit_target,
        stop_loss=settings.stop_loss,
        max_holding_bars=settings.max_holding_bars,
        entry_lag_bars=settings.entry_lag_bars,
        barrier_basis="high_low",
        bootstrap_samples=settings.bootstrap_samples,
        periods=(
            EvaluationPeriod(
                name="development",
                start_at=clean_prices[0].observed_at,
                end_at=clean_prices[split_index - 1].observed_at,
            ),
            EvaluationPeriod(
                name="holdout",
                start_at=clean_prices[split_index].observed_at,
                end_at=clean_prices[-1].observed_at,
            ),
        ),
    )
    study = evaluate_signals(_outcomes(clean_prices), instructions, specification)
    return StrategyEvaluationResult(
        strategy=strategy,
        raw_instructions=raw_instructions,
        instructions=instructions,
        study=study,
    )


def build_chart_rows(
    prices: Sequence[PricePoint],
    result: StrategyEvaluationResult,
    *,
    max_bars: int | None,
) -> list[dict[str, ChartValue]]:
    """Build provider-neutral chart records from prices and evaluated events."""

    selected = tuple(prices if max_bars is None else prices[-max_bars:])
    if not selected:
        return []
    first_timestamp = selected[0].observed_at
    rows: list[dict[str, ChartValue]] = [
        {
            "timestamp": point.observed_at.isoformat(),
            "value": point.close,
            "kind": "price",
            "label": "Close",
            "details": "Observed close",
        }
        for point in selected
    ]
    if result.study is None:
        return rows
    for event in result.study.triple_barrier.events:
        if event.entry_at >= first_timestamp:
            rows.append(
                {
                    "timestamp": event.entry_at.isoformat(),
                    "value": event.entry_value,
                    "kind": "buy" if event.direction == "long" else "sell",
                    "label": "BUY" if event.direction == "long" else "SELL",
                    "details": f"Signal observed {event.signal_at.isoformat()}",
                }
            )
        if event.exit_at >= first_timestamp:
            rows.append(
                {
                    "timestamp": event.exit_at.isoformat(),
                    "value": event.exit_value,
                    "kind": "exit",
                    "label": "EXIT",
                    "details": f"{event.exit_reason}; {event.r_multiple:.2f}R",
                }
            )
    return rows


def _outcomes(prices: Sequence[PricePoint]) -> tuple[OutcomePoint, ...]:
    return tuple(
        OutcomePoint(
            observed_at=point.observed_at,
            value=point.close,
            entry_value=point.open,
            high=point.high,
            low=point.low,
        )
        for point in prices
    )


def _same_session_instructions(
    instructions: Sequence[SignalEvent],
    prices: Sequence[PricePoint],
    *,
    required_forward_bars: int,
) -> tuple[SignalEvent, ...]:
    eligible = set(
        _same_session_candidate_times(
            prices,
            required_forward_bars=required_forward_bars,
        )
    )
    return tuple(
        instruction
        for instruction in instructions
        if instruction.observed_at in eligible
    )


def _same_session_candidate_times(
    prices: Sequence[PricePoint],
    *,
    required_forward_bars: int,
) -> tuple[datetime, ...]:
    timezone = ZoneInfo("America/New_York")
    result: list[datetime] = []
    for index, point in enumerate(prices):
        end_index = index + required_forward_bars
        if end_index >= len(prices):
            continue
        if (
            point.observed_at.astimezone(timezone).date()
            == prices[end_index].observed_at.astimezone(timezone).date()
        ):
            result.append(point.observed_at)
    return tuple(result)
