"""Source-independent historical outcome evaluation for timestamped signals."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from trade_research.domain import (
    AnalysisMethod,
    AnalystResult,
    DerivedAlgorithm,
    InstrumentId,
    LimitationKind,
    MetricKind,
    Observation,
    OutcomeSeriesSpec,
    ReportSignalOutcome,
    ReportStatus,
    SignalDirectionSummary,
    SignalEvaluationPeriodPresentation,
    SignalEvaluationPresentation,
    SignalEvaluationSummary,
    SignalEvent,
    SignalExperimentDefinition,
    SignalKind,
)
from trade_research.domain.provenance import normalize_provider_kind
from trade_research.providers import (
    CapabilityName,
    OutcomePoint,
    OutcomeSeries,
    ProviderRegistry,
)

Direction = Literal["long", "short"]
ChangeKind = Literal["relative", "absolute"]
ExitReason = Literal["fixed_horizon", "profit_target", "stop_loss", "time_limit"]
BarrierBasis = Literal["observed_value", "high_low"]
PeriodName = Literal["development", "validation", "holdout"]


@dataclass(frozen=True, slots=True)
class EvaluationPeriod:
    """A chronology-preserving result window with purged boundary events."""

    name: PeriodName
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        if self.name not in {"development", "validation", "holdout"}:
            raise ValueError("unknown evaluation period name")
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("evaluation period timestamps must include a timezone")
        if self.end_at <= self.start_at:
            raise ValueError("evaluation period end must follow its start")


@dataclass(frozen=True, slots=True)
class ExperimentDefinition:
    """Safe identity for the strategy-generation experiment."""

    experiment_id: str
    strategy_version: str
    strategy_frozen_at: datetime
    evaluation_data_end: datetime
    variant_count: int = 1
    parameters_reference: str | None = None

    def __post_init__(self) -> None:
        SignalExperimentDefinition(
            experiment_id=self.experiment_id,
            strategy_version=self.strategy_version,
            strategy_frozen_at=self.strategy_frozen_at,
            evaluation_data_end=self.evaluation_data_end,
            variant_count=self.variant_count,
            parameters_reference=self.parameters_reference,
        )


@dataclass(frozen=True, slots=True)
class OutcomeSpecification:
    """The fixed, reproducible rules used to judge every signal event."""

    change_kind: ChangeKind
    fixed_horizon_bars: int
    profit_target: float
    stop_loss: float
    max_holding_bars: int
    entry_lag_bars: int = 1
    barrier_basis: BarrierBasis = "observed_value"
    bootstrap_samples: int = 1000
    bootstrap_seed: int = 0
    bootstrap_block_length: int | None = None
    baseline_trials: int = 0
    baseline_seed: int = 0
    baseline_eligible_times: tuple[datetime, ...] = ()
    periods: tuple[EvaluationPeriod, ...] = ()

    def __post_init__(self) -> None:
        if self.change_kind not in {"relative", "absolute"}:
            raise ValueError("change_kind must be relative or absolute")
        if self.fixed_horizon_bars < 1 or self.max_holding_bars < 1:
            raise ValueError("evaluation horizons must be positive")
        if self.entry_lag_bars < 1:
            raise ValueError("entry_lag_bars must be positive")
        if self.barrier_basis not in {"observed_value", "high_low"}:
            raise ValueError("barrier_basis must be observed_value or high_low")
        if not math.isfinite(self.profit_target) or self.profit_target <= 0:
            raise ValueError("profit_target must be positive and finite")
        if not math.isfinite(self.stop_loss) or self.stop_loss <= 0:
            raise ValueError("stop_loss must be positive and finite")
        if self.change_kind == "relative" and (
            self.profit_target >= 1 or self.stop_loss >= 1
        ):
            raise ValueError("relative targets and stops must be less than one")
        if not 100 <= self.bootstrap_samples <= 5000:
            raise ValueError("bootstrap_samples must be between 100 and 5000")
        if not 0 <= self.baseline_trials <= 5000:
            raise ValueError("baseline_trials must be between 0 and 5000")
        if not 0 <= self.bootstrap_seed <= 2**32 - 1:
            raise ValueError("bootstrap_seed must be an unsigned 32-bit integer")
        if self.bootstrap_block_length is not None and not (
            1 <= self.bootstrap_block_length <= 520
        ):
            raise ValueError("bootstrap_block_length must be between 1 and 520")
        if not 0 <= self.baseline_seed <= 2**32 - 1:
            raise ValueError("baseline_seed must be an unsigned 32-bit integer")
        if any(timestamp.tzinfo is None for timestamp in self.baseline_eligible_times):
            raise ValueError("baseline eligible timestamps must include a timezone")
        if list(self.baseline_eligible_times) != sorted(
            self.baseline_eligible_times
        ) or len(set(self.baseline_eligible_times)) != len(
            self.baseline_eligible_times
        ):
            raise ValueError("baseline eligible timestamps must be ordered and unique")
        _validate_periods(self.periods)


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    """One completed, causally aligned historical signal outcome."""

    signal_at: datetime
    entry_at: datetime
    exit_at: datetime
    direction: Direction
    entry_value: float
    exit_value: float
    change: float
    initial_risk: float
    r_multiple: float
    maximum_favorable_change: float
    maximum_adverse_change: float
    duration_bars: int
    exit_reason: ExitReason
    same_bar_ambiguous: bool
    entry_index: int
    exit_index: int


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    """Descriptive performance and conservative independence metadata."""

    events: tuple[SignalOutcome, ...]
    skipped_event_count: int
    purged_event_count: int
    event_count: int
    non_overlapping_event_count: int
    win_count: int
    loss_count: int
    breakeven_count: int
    win_rate: float | None
    non_overlapping_win_rate: float | None
    non_overlapping_win_rate_lower_95: float | None
    average_win: float | None
    average_loss: float | None
    average_win_r: float | None
    average_loss_r: float | None
    reward_risk_ratio: float | None
    win_payoff_product: float | None
    break_even_win_rate: float | None
    edge_over_break_even: float | None
    expected_value: float | None
    expected_r: float | None
    non_overlapping_expected_r: float | None
    non_overlapping_expected_r_lower_95: float | None
    non_overlapping_expected_r_median: float | None
    non_overlapping_expected_r_upper_95: float | None
    bootstrap_positive_fraction: float | None
    bootstrap_block_length: int | None
    profit_factor: float | None
    top_five_win_contribution: float | None
    baseline_trial_count: int
    baseline_expected_r: float | None
    baseline_expected_r_lower_95: float | None
    baseline_expected_r_upper_95: float | None
    excess_expected_r: float | None
    direction_summaries: tuple[tuple[Direction, int, float | None, float | None], ...]


@dataclass(frozen=True, slots=True)
class PeriodEvaluation:
    """Fixed and barrier results wholly contained inside one period."""

    name: PeriodName
    start_at: datetime
    end_at: datetime
    fixed_horizon: EvaluationSummary
    triple_barrier: EvaluationSummary


@dataclass(frozen=True, slots=True)
class SignalEvaluationStudy:
    """Fixed-horizon and path-dependent views of the same signal stream."""

    fixed_horizon: EvaluationSummary
    triple_barrier: EvaluationSummary
    periods: tuple[PeriodEvaluation, ...] = ()


@dataclass(frozen=True, slots=True)
class FixedHorizonOutcome:
    """One signal outcome without imposing a stop or target assumption."""

    signal_at: datetime
    entry_at: datetime
    exit_at: datetime
    change: float
    maximum_favorable_change: float
    maximum_adverse_change: float


@dataclass(frozen=True, slots=True)
class FixedHorizonStudy:
    """Minimal entry-signal study shared by backtests and standalone evaluation."""

    outcomes: tuple[FixedHorizonOutcome, ...]
    skipped_event_count: int


def evaluate_fixed_horizon_signals(
    points: tuple[OutcomePoint, ...],
    instructions: tuple[SignalEvent, ...],
    *,
    horizon_bars: int,
    entry_lag_bars: int = 1,
    change_kind: ChangeKind = "relative",
    barrier_basis: BarrierBasis = "high_low",
) -> FixedHorizonStudy:
    """Evaluate signal entries without silently selecting protective barriers."""

    if horizon_bars < 1 or entry_lag_bars < 1:
        raise ValueError("evaluation horizons must be positive")
    if change_kind not in {"relative", "absolute"}:
        raise ValueError("change_kind must be relative or absolute")
    if barrier_basis not in {"observed_value", "high_low"}:
        raise ValueError("barrier_basis must be observed_value or high_low")
    point_times = [point.observed_at for point in points]
    signal_times = [instruction.observed_at for instruction in instructions]
    if not points or point_times != sorted(point_times) or len(set(point_times)) != len(
        point_times
    ):
        raise ValueError("outcome points must be non-empty, ordered, and unique")
    if signal_times != sorted(signal_times) or len(set(signal_times)) != len(signal_times):
        raise ValueError("signal instructions must be ordered and unique")
    if any(not instruction.is_entry for instruction in instructions):
        raise ValueError("signal evaluation requires entry events")
    indexes = {point.observed_at: index for index, point in enumerate(points)}
    if any(timestamp not in indexes for timestamp in signal_times):
        raise ValueError("signal instructions must align exactly to outcome points")
    if change_kind == "relative" and any(point.lower <= 0 for point in points):
        raise ValueError("relative outcome evaluation requires positive values")

    outcomes: list[FixedHorizonOutcome] = []
    skipped = 0
    for instruction in instructions:
        entry_index = indexes[instruction.observed_at] + entry_lag_bars
        exit_index = entry_index + horizon_bars
        if exit_index >= len(points):
            skipped += 1
            continue
        entry = points[entry_index]
        exit_point = points[exit_index]
        favorable, adverse = _excursions(
            points,
            entry_index,
            exit_index,
            instruction.direction,
            change_kind,
            barrier_basis,
        )
        outcomes.append(
            FixedHorizonOutcome(
                signal_at=instruction.observed_at,
                entry_at=entry.observed_at,
                exit_at=exit_point.observed_at,
                change=_directional_change(
                    entry.execution_value,
                    exit_point.value,
                    instruction.direction,
                    change_kind,
                ),
                maximum_favorable_change=favorable,
                maximum_adverse_change=adverse,
            )
        )
    return FixedHorizonStudy(tuple(outcomes), skipped)


def evaluate_signals(
    points: tuple[OutcomePoint, ...],
    instructions: tuple[SignalEvent, ...],
    specification: OutcomeSpecification,
) -> SignalEvaluationStudy:
    """Evaluate deterministic signals without depending on their data sources."""

    _validate_inputs(points, instructions, specification)
    indexes = {point.observed_at: index for index, point in enumerate(points)}
    fixed_events: list[SignalOutcome] = []
    barrier_events: list[SignalOutcome] = []
    fixed_skipped = 0
    barrier_skipped = 0

    for instruction in instructions:
        signal_index = indexes[instruction.observed_at]
        entry_index = signal_index + specification.entry_lag_bars
        fixed_exit_index = entry_index + specification.fixed_horizon_bars
        if entry_index >= len(points) or fixed_exit_index >= len(points):
            fixed_skipped += 1
        else:
            fixed_events.append(
                _fixed_outcome(
                    points,
                    instruction,
                    entry_index,
                    fixed_exit_index,
                    specification.change_kind,
                    specification.stop_loss,
                    specification.barrier_basis,
                )
            )

        if entry_index >= len(points):
            barrier_skipped += 1
        else:
            requested_end = entry_index + specification.max_holding_bars
            available_end = min(requested_end, len(points) - 1)
            outcome = _barrier_outcome(
                points,
                instruction,
                entry_index,
                available_end,
                specification,
                horizon_complete=requested_end < len(points),
            )
            if outcome is None:
                barrier_skipped += 1
            else:
                barrier_events.append(outcome)

    fixed_summary = _summarize(
        tuple(fixed_events),
        fixed_skipped,
        0,
        specification.fixed_horizon_bars,
        bootstrap_samples=specification.bootstrap_samples,
        bootstrap_seed=specification.bootstrap_seed,
        bootstrap_block_length=specification.bootstrap_block_length,
    )
    barrier_summary = _summarize(
        tuple(barrier_events),
        barrier_skipped,
        0,
        specification.max_holding_bars,
        bootstrap_samples=specification.bootstrap_samples,
        bootstrap_seed=specification.bootstrap_seed + 1,
        bootstrap_block_length=specification.bootstrap_block_length,
    )
    if specification.baseline_trials:
        fixed_summary = _with_matched_baseline(
            fixed_summary,
            points,
            specification,
            view="fixed_horizon",
        )
        barrier_summary = _with_matched_baseline(
            barrier_summary,
            points,
            specification,
            view="triple_barrier",
        )
    periods = _period_evaluations(
        fixed_summary,
        barrier_summary,
        points,
        instructions,
        specification,
    )
    return SignalEvaluationStudy(
        fixed_horizon=fixed_summary,
        triple_barrier=barrier_summary,
        periods=periods,
    )


def _validate_inputs(
    points: tuple[OutcomePoint, ...],
    instructions: tuple[SignalEvent, ...],
    specification: OutcomeSpecification,
) -> None:
    if not points:
        raise ValueError("at least one outcome point is required")
    if not instructions:
        raise ValueError("at least one signal instruction is required")
    point_times = [point.observed_at for point in points]
    if point_times != sorted(point_times) or len(set(point_times)) != len(point_times):
        raise ValueError("outcome points must have ordered unique timestamps")
    signal_times = [instruction.observed_at for instruction in instructions]
    if signal_times != sorted(signal_times) or len(set(signal_times)) != len(signal_times):
        raise ValueError("signal instructions must have ordered unique timestamps")
    if any(timestamp not in set(point_times) for timestamp in signal_times):
        raise ValueError("signal instructions must align exactly to outcome points")
    if any(not instruction.is_entry for instruction in instructions):
        raise ValueError("signal evaluation requires entry events")
    if specification.change_kind == "relative" and any(
        point.lower <= 0 for point in points
    ):
        raise ValueError("relative outcome evaluation requires positive values")
    if specification.change_kind == "relative" and any(
        instruction.initial_risk is not None and instruction.initial_risk >= 1
        for instruction in instructions
    ):
        raise ValueError("relative initial_risk must be less than one")
    point_time_set = set(point_times)
    if any(
        timestamp not in point_time_set
        for timestamp in specification.baseline_eligible_times
    ):
        raise ValueError("baseline eligible timestamps must align to outcome points")


def _fixed_outcome(
    points: tuple[OutcomePoint, ...],
    instruction: SignalEvent,
    entry_index: int,
    exit_index: int,
    change_kind: ChangeKind,
    default_initial_risk: float,
    barrier_basis: BarrierBasis,
) -> SignalOutcome:
    entry = points[entry_index]
    exit_point = points[exit_index]
    change = _directional_change(
        entry.execution_value, exit_point.value, instruction.direction, change_kind
    )
    initial_risk = instruction.initial_risk or default_initial_risk
    favorable, adverse = _excursions(
        points,
        entry_index,
        exit_index,
        instruction.direction,
        change_kind,
        barrier_basis,
    )
    return SignalOutcome(
        signal_at=instruction.observed_at,
        entry_at=entry.observed_at,
        exit_at=exit_point.observed_at,
        direction=instruction.direction,
        entry_value=entry.execution_value,
        exit_value=exit_point.value,
        change=change,
        initial_risk=initial_risk,
        r_multiple=change / initial_risk,
        maximum_favorable_change=favorable,
        maximum_adverse_change=adverse,
        duration_bars=exit_index - entry_index,
        exit_reason="fixed_horizon",
        same_bar_ambiguous=False,
        entry_index=entry_index,
        exit_index=exit_index,
    )


def _barrier_outcome(
    points: tuple[OutcomePoint, ...],
    instruction: SignalEvent,
    entry_index: int,
    end_index: int,
    specification: OutcomeSpecification,
    *,
    horizon_complete: bool,
) -> SignalOutcome | None:
    entry = points[entry_index]
    initial_risk = instruction.initial_risk or specification.stop_loss
    for exit_index in range(entry_index + 1, end_index + 1):
        point = points[exit_index]
        if specification.barrier_basis == "high_low":
            favorable_value = point.upper if instruction.direction == "long" else point.lower
            adverse_value = point.lower if instruction.direction == "long" else point.upper
        else:
            favorable_value = point.value
            adverse_value = point.value
        favorable = _directional_change(
            entry.execution_value,
            favorable_value,
            instruction.direction,
            specification.change_kind,
        )
        adverse = _directional_change(
            entry.execution_value,
            adverse_value,
            instruction.direction,
            specification.change_kind,
        )
        hit_target = favorable >= specification.profit_target
        hit_stop = adverse <= -initial_risk
        if not hit_target and not hit_stop:
            continue
        # Daily and snapshot data cannot reveal which barrier traded first.
        # Scoring the loss is deterministic and avoids optimistic path assumptions.
        is_loss = hit_stop
        change = -initial_risk if is_loss else specification.profit_target
        exit_value = _barrier_value(
            entry.execution_value,
            change,
            instruction.direction,
            specification.change_kind,
        )
        favorable_excursion, adverse_excursion = _excursions(
            points,
            entry_index,
            exit_index,
            instruction.direction,
            specification.change_kind,
            specification.barrier_basis,
        )
        return SignalOutcome(
            signal_at=instruction.observed_at,
            entry_at=entry.observed_at,
            exit_at=point.observed_at,
            direction=instruction.direction,
            entry_value=entry.execution_value,
            exit_value=exit_value,
            change=change,
            initial_risk=initial_risk,
            r_multiple=change / initial_risk,
            maximum_favorable_change=favorable_excursion,
            maximum_adverse_change=adverse_excursion,
            duration_bars=exit_index - entry_index,
            exit_reason="stop_loss" if is_loss else "profit_target",
            same_bar_ambiguous=hit_target and hit_stop,
            entry_index=entry_index,
            exit_index=exit_index,
        )

    if not horizon_complete:
        return None
    exit_point = points[end_index]
    change = _directional_change(
        entry.execution_value,
        exit_point.value,
        instruction.direction,
        specification.change_kind,
    )
    favorable_excursion, adverse_excursion = _excursions(
        points,
        entry_index,
        end_index,
        instruction.direction,
        specification.change_kind,
        specification.barrier_basis,
    )
    return SignalOutcome(
        signal_at=instruction.observed_at,
        entry_at=entry.observed_at,
        exit_at=exit_point.observed_at,
        direction=instruction.direction,
        entry_value=entry.execution_value,
        exit_value=exit_point.value,
        change=change,
        initial_risk=initial_risk,
        r_multiple=change / initial_risk,
        maximum_favorable_change=favorable_excursion,
        maximum_adverse_change=adverse_excursion,
        duration_bars=end_index - entry_index,
        exit_reason="time_limit",
        same_bar_ambiguous=False,
        entry_index=entry_index,
        exit_index=end_index,
    )


def _directional_change(
    entry: float, mark: float, direction: Direction, change_kind: ChangeKind
) -> float:
    raw = mark / entry - 1 if change_kind == "relative" else mark - entry
    return raw if direction == "long" else -raw


def _barrier_value(
    entry: float, change: float, direction: Direction, change_kind: ChangeKind
) -> float:
    signed_change = change if direction == "long" else -change
    if change_kind == "relative":
        return entry * (1 + signed_change)
    return entry + signed_change


def _excursions(
    points: tuple[OutcomePoint, ...],
    entry_index: int,
    exit_index: int,
    direction: Direction,
    change_kind: ChangeKind,
    barrier_basis: BarrierBasis,
) -> tuple[float, float]:
    entry = points[entry_index].execution_value
    favorable_changes = [0.0]
    adverse_changes = [0.0]
    for point in points[entry_index + 1 : exit_index + 1]:
        if barrier_basis == "high_low":
            favorable_value = point.upper if direction == "long" else point.lower
            adverse_value = point.lower if direction == "long" else point.upper
        else:
            favorable_value = point.value
            adverse_value = point.value
        favorable_changes.append(
            _directional_change(entry, favorable_value, direction, change_kind)
        )
        adverse_changes.append(
            _directional_change(entry, adverse_value, direction, change_kind)
        )
    return max(favorable_changes), min(adverse_changes)


def _summarize(
    events: tuple[SignalOutcome, ...],
    skipped_event_count: int,
    purged_event_count: int,
    embargo_bars: int,
    *,
    bootstrap_samples: int,
    bootstrap_seed: int,
    bootstrap_block_length: int | None,
) -> EvaluationSummary:
    changes = [event.change for event in events]
    wins = [change for change in changes if change > 0]
    losses = [change for change in changes if change < 0]
    r_multiples = [event.r_multiple for event in events]
    wins_r = [value for value in r_multiples if value > 0]
    losses_r = [value for value in r_multiples if value < 0]
    breakeven_count = len(changes) - len(wins) - len(losses)
    non_overlapping = _non_overlapping_events(events, embargo_bars)
    non_overlapping_wins = sum(event.change > 0 for event in non_overlapping)
    non_overlapping_r = [event.r_multiple for event in non_overlapping]
    win_rate = len(wins) / len(changes) if changes else None
    non_overlapping_win_rate = (
        non_overlapping_wins / len(non_overlapping) if non_overlapping else None
    )
    average_win = statistics.fmean(wins) if wins else None
    average_loss = abs(statistics.fmean(losses)) if losses else None
    average_win_r = statistics.fmean(wins_r) if wins_r else None
    average_loss_r = abs(statistics.fmean(losses_r)) if losses_r else None
    reward_risk = (
        average_win_r / average_loss_r
        if average_win_r is not None and average_loss_r is not None
        else None
    )
    breakeven_rate = breakeven_count / len(changes) if changes else 0.0
    break_even = (
        (1 - breakeven_rate)
        * average_loss_r
        / (average_win_r + average_loss_r)
        if average_win_r is not None and average_loss_r is not None
        else None
    )
    bootstrap = _bootstrap_expected_r(
        non_overlapping_r,
        samples=bootstrap_samples,
        seed=bootstrap_seed,
        block_length=bootstrap_block_length,
    )
    loss_sum_r = abs(sum(losses_r))
    profit_factor = sum(wins_r) / loss_sum_r if wins_r and loss_sum_r > 0 else None
    win_total = sum(wins_r)
    top_five_contribution = (
        sum(sorted(wins_r, reverse=True)[:5]) / win_total
        if win_total > 0
        else None
    )
    direction_summary_rows: list[
        tuple[Direction, int, float | None, float | None]
    ] = []
    for direction in ("long", "short"):
        selected = tuple(event for event in events if event.direction == direction)
        if selected:
            direction_summary_rows.append(
                (
                    direction,
                    len(selected),
                    sum(event.change > 0 for event in selected) / len(selected),
                    statistics.fmean(event.r_multiple for event in selected),
                )
            )
    return EvaluationSummary(
        events=events,
        skipped_event_count=skipped_event_count,
        purged_event_count=purged_event_count,
        event_count=len(events),
        non_overlapping_event_count=len(non_overlapping),
        win_count=len(wins),
        loss_count=len(losses),
        breakeven_count=breakeven_count,
        win_rate=win_rate,
        non_overlapping_win_rate=non_overlapping_win_rate,
        non_overlapping_win_rate_lower_95=(
            _wilson_lower(non_overlapping_wins, len(non_overlapping))
            if non_overlapping
            else None
        ),
        average_win=average_win,
        average_loss=average_loss,
        average_win_r=average_win_r,
        average_loss_r=average_loss_r,
        reward_risk_ratio=reward_risk,
        win_payoff_product=(
            win_rate * reward_risk
            if win_rate is not None and reward_risk is not None
            else None
        ),
        break_even_win_rate=break_even,
        edge_over_break_even=(
            win_rate - break_even
            if win_rate is not None and break_even is not None
            else None
        ),
        expected_value=statistics.fmean(changes) if changes else None,
        expected_r=statistics.fmean(r_multiples) if r_multiples else None,
        non_overlapping_expected_r=(
            statistics.fmean(non_overlapping_r) if non_overlapping_r else None
        ),
        non_overlapping_expected_r_lower_95=bootstrap[0],
        non_overlapping_expected_r_median=bootstrap[1],
        non_overlapping_expected_r_upper_95=bootstrap[2],
        bootstrap_positive_fraction=bootstrap[3],
        bootstrap_block_length=bootstrap[4],
        profit_factor=profit_factor,
        top_five_win_contribution=top_five_contribution,
        baseline_trial_count=0,
        baseline_expected_r=None,
        baseline_expected_r_lower_95=None,
        baseline_expected_r_upper_95=None,
        excess_expected_r=None,
        direction_summaries=tuple(direction_summary_rows),
    )


def _bootstrap_expected_r(
    values: list[float], *, samples: int, seed: int, block_length: int | None
) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    int | None,
]:
    if len(values) < 2:
        return None, None, None, None, None
    generator = random.Random(seed)
    effective_block_length = min(
        block_length or max(1, round(math.sqrt(len(values)))), len(values)
    )
    means = sorted(
        statistics.fmean(
            _circular_block_resample(
                values,
                generator,
                block_length=effective_block_length,
            )
        )
        for _ in range(samples)
    )
    return (
        _percentile(means, 0.025),
        _percentile(means, 0.5),
        _percentile(means, 0.975),
        sum(value > 0 for value in means) / len(means),
        effective_block_length,
    )


def _circular_block_resample(
    values: list[float], generator: random.Random, *, block_length: int
) -> list[float]:
    result: list[float] = []
    while len(result) < len(values):
        start = generator.randrange(len(values))
        result.extend(
            values[(start + offset) % len(values)] for offset in range(block_length)
        )
    return result[: len(values)]


def _percentile(values: list[float], probability: float) -> float:
    position = probability * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _non_overlapping_events(
    events: tuple[SignalOutcome, ...], embargo_bars: int
) -> tuple[SignalOutcome, ...]:
    selected: list[SignalOutcome] = []
    last_exit = -1
    for event in events:
        if event.entry_index > last_exit:
            selected.append(event)
            last_exit = event.entry_index + embargo_bars
    return tuple(selected)


def _with_matched_baseline(
    summary: EvaluationSummary,
    points: tuple[OutcomePoint, ...],
    specification: OutcomeSpecification,
    *,
    view: Literal["fixed_horizon", "triple_barrier"],
) -> EvaluationSummary:
    outcome_horizon = (
        specification.fixed_horizon_bars
        if view == "fixed_horizon"
        else specification.max_holding_bars
    )
    required_forward = specification.entry_lag_bars + outcome_horizon
    eligible_time_set = set(specification.baseline_eligible_times)
    completed_signal_times = [event.signal_at for event in summary.events]
    if not completed_signal_times:
        return summary
    first_signal = min(completed_signal_times)
    last_signal = max(completed_signal_times)
    eligible_indexes = [
        index
        for index in range(max(0, len(points) - required_forward))
        if (
            points[index].observed_at in eligible_time_set
            if eligible_time_set
            else first_signal <= points[index].observed_at <= last_signal
        )
    ]
    sample_size = len(summary.events)
    if sample_size == 0 or len(eligible_indexes) < sample_size:
        return summary
    profiles = [
        (event.direction, event.initial_risk) for event in summary.events
    ]
    generator = random.Random(
        specification.baseline_seed + (0 if view == "fixed_horizon" else 1)
    )
    baseline_values: list[float] = []
    baseline_specification = replace(
        specification,
        bootstrap_samples=100,
        baseline_trials=0,
        baseline_eligible_times=(),
        periods=(),
    )
    for _ in range(specification.baseline_trials):
        selected_indexes = sorted(generator.sample(eligible_indexes, sample_size))
        selected_profiles = profiles.copy()
        generator.shuffle(selected_profiles)
        baseline_instructions = tuple(
            SignalEvent(
                observed_at=points[index].observed_at,
                action=(
                    "add_long"
                    if selected_profiles[offset][0] == "long"
                    else "add_short"
                ),
                initial_risk=selected_profiles[offset][1],
            )
            for offset, index in enumerate(selected_indexes)
        )
        baseline_study = evaluate_signals(
            points,
            baseline_instructions,
            baseline_specification,
        )
        baseline_summary = getattr(baseline_study, view)
        if baseline_summary.non_overlapping_expected_r is not None:
            baseline_values.append(baseline_summary.non_overlapping_expected_r)
    if not baseline_values:
        return summary
    ordered = sorted(baseline_values)
    baseline_expected = statistics.fmean(ordered)
    return replace(
        summary,
        baseline_trial_count=len(ordered),
        baseline_expected_r=baseline_expected,
        baseline_expected_r_lower_95=_percentile(ordered, 0.025),
        baseline_expected_r_upper_95=_percentile(ordered, 0.975),
        excess_expected_r=(
            summary.non_overlapping_expected_r - baseline_expected
            if summary.non_overlapping_expected_r is not None
            else None
        ),
    )


def _period_evaluations(
    fixed_summary: EvaluationSummary,
    barrier_summary: EvaluationSummary,
    points: tuple[OutcomePoint, ...],
    instructions: tuple[SignalEvent, ...],
    specification: OutcomeSpecification,
) -> tuple[PeriodEvaluation, ...]:
    result: list[PeriodEvaluation] = []
    for index, period in enumerate(specification.periods):
        period_instructions = tuple(
            instruction
            for instruction in instructions
            if period.start_at <= instruction.observed_at <= period.end_at
        )
        fixed_started = tuple(
            event
            for event in fixed_summary.events
            if period.start_at <= event.signal_at <= period.end_at
        )
        barrier_started = tuple(
            event
            for event in barrier_summary.events
            if period.start_at <= event.signal_at <= period.end_at
        )
        fixed_events = tuple(
            event
            for event in fixed_started
            if event.exit_at <= period.end_at
        )
        barrier_events = tuple(
            event
            for event in barrier_started
            if event.exit_at <= period.end_at
        )
        fixed_skipped = len(period_instructions) - len(fixed_started)
        barrier_skipped = len(period_instructions) - len(barrier_started)
        fixed_purged = len(fixed_started) - len(fixed_events)
        barrier_purged = len(barrier_started) - len(barrier_events)
        fixed_period = _summarize(
            fixed_events,
            fixed_skipped,
            fixed_purged,
            specification.fixed_horizon_bars,
            bootstrap_samples=specification.bootstrap_samples,
            bootstrap_seed=specification.bootstrap_seed + 10 + index * 2,
            bootstrap_block_length=specification.bootstrap_block_length,
        )
        barrier_period = _summarize(
            barrier_events,
            barrier_skipped,
            barrier_purged,
            specification.max_holding_bars,
            bootstrap_samples=specification.bootstrap_samples,
            bootstrap_seed=specification.bootstrap_seed + 11 + index * 2,
            bootstrap_block_length=specification.bootstrap_block_length,
        )
        period_points = tuple(
            point
            for point in points
            if period.start_at <= point.observed_at <= period.end_at
        )
        if specification.baseline_trials and period_points and period_instructions:
            period_specification = replace(specification, periods=())
            fixed_period = _with_matched_baseline(
                fixed_period,
                period_points,
                period_specification,
                view="fixed_horizon",
            )
            barrier_period = _with_matched_baseline(
                barrier_period,
                period_points,
                period_specification,
                view="triple_barrier",
            )
        result.append(
            PeriodEvaluation(
                name=period.name,
                start_at=period.start_at,
                end_at=period.end_at,
                fixed_horizon=fixed_period,
                triple_barrier=barrier_period,
            )
        )
    return tuple(result)


def _validate_periods(periods: tuple[EvaluationPeriod, ...]) -> None:
    names = [period.name for period in periods]
    if len(set(names)) != len(names):
        raise ValueError("evaluation period names must be unique")
    expected_order = {"development": 0, "validation": 1, "holdout": 2}
    if names != sorted(names, key=expected_order.__getitem__):
        raise ValueError("evaluation periods must follow development, validation, holdout")
    for previous, current in zip(periods, periods[1:], strict=False):
        if current.start_at <= previous.end_at:
            raise ValueError("evaluation periods must not overlap")


def _wilson_lower(successes: int, count: int) -> float:
    z = 1.959963984540054
    proportion = successes / count
    denominator = 1 + z**2 / count
    center = proportion + z**2 / (2 * count)
    adjustment = z * math.sqrt(
        proportion * (1 - proportion) / count + z**2 / (4 * count**2)
    )
    return max(0.0, (center - adjustment) / denominator)


@dataclass(frozen=True, slots=True)
class SignalEvaluationSkill:
    """Evaluate supplied signals independently of how those signals were produced."""

    signal_name: str = "external-signal"
    instructions: tuple[SignalEvent, ...] = ()
    change_kind: ChangeKind = "relative"
    fixed_horizon_bars: int = 21
    profit_target: float = 0.06
    stop_loss: float = 0.03
    max_holding_bars: int = 63
    entry_lag_bars: int = 1
    bootstrap_samples: int = 1000
    bootstrap_seed: int = 0
    bootstrap_block_length: int | None = None
    baseline_trials: int = 100
    baseline_seed: int = 0
    baseline_eligible_times: tuple[datetime, ...] = ()
    periods: tuple[EvaluationPeriod, ...] = ()
    experiment: ExperimentDefinition | None = None
    target_series: OutcomeSeriesSpec = field(
        default_factory=lambda: OutcomeSeriesSpec(
            name="close",
            kind="price",
            unit="price",
        )
    )
    _name: str = field(default="signal-evaluation", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.OUTCOMES,)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        series = providers.outcomes(instrument, self.target_series)
        if not self.instructions or not series.points:
            return _partial_result(instrument)
        experiment = self.experiment or ExperimentDefinition(
            experiment_id=self.signal_name,
            strategy_version="unversioned",
            strategy_frozen_at=series.points[-1].observed_at,
            evaluation_data_end=series.points[-1].observed_at,
        )
        if any(
            instruction.observed_at > experiment.evaluation_data_end
            for instruction in self.instructions
        ):
            raise ValueError("signal instructions cannot follow evaluation_data_end")
        if any(period.end_at > experiment.evaluation_data_end for period in self.periods):
            raise ValueError("evaluation periods cannot follow evaluation_data_end")
        series = replace(
            series,
            points=tuple(
                point
                for point in series.points
                if point.observed_at <= experiment.evaluation_data_end
            ),
        )
        if not series.points:
            raise ValueError("evaluation_data_end precedes the available outcome series")
        specification = OutcomeSpecification(
            change_kind=self.change_kind,
            fixed_horizon_bars=self.fixed_horizon_bars,
            profit_target=self.profit_target,
            stop_loss=self.stop_loss,
            max_holding_bars=self.max_holding_bars,
            entry_lag_bars=self.entry_lag_bars,
            barrier_basis=series.barrier_basis,
            bootstrap_samples=self.bootstrap_samples,
            bootstrap_seed=self.bootstrap_seed,
            bootstrap_block_length=self.bootstrap_block_length,
            baseline_trials=self.baseline_trials,
            baseline_seed=self.baseline_seed,
            baseline_eligible_times=self.baseline_eligible_times,
            periods=self.periods,
        )
        study = evaluate_signals(series.points, self.instructions, specification)
        signal_reference = _signal_reference(self.instructions)
        series_reference = _outcome_series_reference(series)
        configuration_reference = _configuration_reference(
            self, series.barrier_basis, signal_reference, experiment
        )
        presentation = SignalEvaluationPresentation(
            signal_name=self.signal_name,
            experiment=_report_experiment(experiment, self.periods),
            target_series=self.target_series,
            change_kind=self.change_kind,
            barrier_basis=series.barrier_basis,
            entry_lag_bars=self.entry_lag_bars,
            fixed_horizon_bars=self.fixed_horizon_bars,
            profit_target=self.profit_target,
            stop_loss=self.stop_loss,
            max_holding_bars=self.max_holding_bars,
            signal_reference=signal_reference,
            series_reference=series_reference,
            configuration_reference=configuration_reference,
            fixed_horizon=_report_summary(study.fixed_horizon),
            triple_barrier=_report_summary(study.triple_barrier),
            periods=tuple(
                SignalEvaluationPeriodPresentation(
                    name=period.name,
                    start_at=period.start_at,
                    end_at=period.end_at,
                    fixed_horizon=_report_summary(period.fixed_horizon),
                    triple_barrier=_report_summary(period.triple_barrier),
                )
                for period in study.periods
            ),
        )
        skipped = (
            study.fixed_horizon.skipped_event_count
            + study.triple_barrier.skipped_event_count
        )
        limitations: list[LimitationKind] = []
        if skipped:
            limitations.append(LimitationKind.INSUFFICIENT_HISTORY)
        if (
            presentation.fixed_horizon.presentation_reduced
            or presentation.triple_barrier.presentation_reduced
        ):
            limitations.append(LimitationKind.BOUNDED_INPUT)
        observations = _metric_observations(
            instrument,
            series,
            study,
            signal_reference=signal_reference,
            series_reference=series_reference,
            configuration_reference=configuration_reference,
            fixed_horizon_bars=self.fixed_horizon_bars,
            max_holding_bars=self.max_holding_bars,
        )
        evaluated = study.fixed_horizon.event_count + study.triple_barrier.event_count
        summary = (
            "complete data: fixed-horizon and triple-barrier signal outcomes evaluated"
            if not limitations
            else "partial data: signal outcomes evaluated with bounded historical coverage"
        )
        if evaluated == 0:
            summary = "partial data: no signal had sufficient forward history"
            if LimitationKind.INSUFFICIENT_HISTORY not in limitations:
                limitations.append(LimitationKind.INSUFFICIENT_HISTORY)
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=summary,
            status=ReportStatus.PARTIAL if limitations else ReportStatus.COMPLETE,
            limitations=tuple(dict.fromkeys(limitations)),
            methods=(
                AnalysisMethod(
                    algorithm=DerivedAlgorithm.SIGNAL_OUTCOME_EVALUATION,
                    window=f"fixed_{self.fixed_horizon_bars}_triple_{self.max_holding_bars}",
                ),
            ),
            signal=SignalKind.NOT_ASSESSED,
            observations=observations,
            presentation=presentation,
        )


def _partial_result(instrument: InstrumentId) -> AnalystResult:
    return AnalystResult(
        analyst="signal-evaluation",
        instrument=instrument,
        summary="partial data: signals and an outcome series are required",
        status=ReportStatus.PARTIAL,
        limitations=(LimitationKind.MISSING_INPUTS,),
        signal=SignalKind.NOT_ASSESSED,
    )


def _report_summary(summary: EvaluationSummary) -> SignalEvaluationSummary:
    reduced = len(summary.events) > 200
    events = summary.events
    if reduced:
        events = events[:100] + events[-100:]
    return SignalEvaluationSummary(
        event_count=summary.event_count,
        non_overlapping_event_count=summary.non_overlapping_event_count,
        skipped_event_count=summary.skipped_event_count,
        purged_event_count=summary.purged_event_count,
        win_count=summary.win_count,
        loss_count=summary.loss_count,
        breakeven_count=summary.breakeven_count,
        win_rate=summary.win_rate,
        non_overlapping_win_rate=summary.non_overlapping_win_rate,
        non_overlapping_win_rate_lower_95=(
            summary.non_overlapping_win_rate_lower_95
        ),
        average_win=summary.average_win,
        average_loss=summary.average_loss,
        average_win_r=summary.average_win_r,
        average_loss_r=summary.average_loss_r,
        reward_risk_ratio=summary.reward_risk_ratio,
        win_payoff_product=summary.win_payoff_product,
        break_even_win_rate=summary.break_even_win_rate,
        edge_over_break_even=summary.edge_over_break_even,
        expected_value=summary.expected_value,
        expected_r=summary.expected_r,
        non_overlapping_expected_r=summary.non_overlapping_expected_r,
        non_overlapping_expected_r_lower_95=(
            summary.non_overlapping_expected_r_lower_95
        ),
        non_overlapping_expected_r_median=(
            summary.non_overlapping_expected_r_median
        ),
        non_overlapping_expected_r_upper_95=(
            summary.non_overlapping_expected_r_upper_95
        ),
        bootstrap_positive_fraction=summary.bootstrap_positive_fraction,
        bootstrap_block_length=summary.bootstrap_block_length,
        profit_factor=summary.profit_factor,
        top_five_win_contribution=summary.top_five_win_contribution,
        baseline_trial_count=summary.baseline_trial_count,
        baseline_expected_r=summary.baseline_expected_r,
        baseline_expected_r_lower_95=summary.baseline_expected_r_lower_95,
        baseline_expected_r_upper_95=summary.baseline_expected_r_upper_95,
        excess_expected_r=summary.excess_expected_r,
        directions=tuple(
            SignalDirectionSummary(
                direction=direction,
                event_count=event_count,
                win_rate=win_rate,
                expected_r=expected_r,
            )
            for direction, event_count, win_rate, expected_r in summary.direction_summaries
        ),
        events=tuple(
            ReportSignalOutcome(
                signal_at=event.signal_at,
                entry_at=event.entry_at,
                exit_at=event.exit_at,
                direction=event.direction,
                entry_value=event.entry_value,
                exit_value=event.exit_value,
                change=event.change,
                initial_risk=event.initial_risk,
                r_multiple=event.r_multiple,
                maximum_favorable_change=event.maximum_favorable_change,
                maximum_adverse_change=event.maximum_adverse_change,
                maximum_favorable_r=(
                    event.maximum_favorable_change / event.initial_risk
                ),
                maximum_adverse_r=(
                    event.maximum_adverse_change / event.initial_risk
                ),
                duration_bars=event.duration_bars,
                exit_reason=event.exit_reason,
                same_bar_ambiguous=event.same_bar_ambiguous,
            )
            for event in events
        ),
        presentation_reduced=reduced,
    )


def _signal_reference(instructions: tuple[SignalEvent, ...]) -> str:
    rows = [
        {
            "observed_at": event.observed_at.isoformat(),
            "direction": event.direction,
            "initial_risk": event.initial_risk,
        }
        for event in instructions
    ]
    payload = json.dumps(rows, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _outcome_series_reference(series: OutcomeSeries) -> str:
    rows = [
        {
            "observed_at": point.observed_at.isoformat(),
            "value": point.value,
            "high": point.high,
            "low": point.low,
        }
        for point in series.points
    ]
    return _sha256(
        {
            "spec": series.spec.model_dump(mode="json"),
            "source": normalize_provider_kind(series.source).value,
            "provider_reference": series.provenance.get("reference"),
            "barrier_basis": series.barrier_basis,
            "points": rows,
        }
    )


def _configuration_reference(
    skill: SignalEvaluationSkill,
    barrier_basis: BarrierBasis,
    signal_reference: str,
    experiment: ExperimentDefinition,
) -> str:
    return _sha256(
        {
            "signal_name": skill.signal_name,
            "signal_reference": signal_reference,
            "target_series": skill.target_series.model_dump(mode="json"),
            "change_kind": skill.change_kind,
            "barrier_basis": barrier_basis,
            "entry_lag_bars": skill.entry_lag_bars,
            "fixed_horizon_bars": skill.fixed_horizon_bars,
            "profit_target": skill.profit_target,
            "stop_loss": skill.stop_loss,
            "max_holding_bars": skill.max_holding_bars,
            "bootstrap_samples": skill.bootstrap_samples,
            "bootstrap_seed": skill.bootstrap_seed,
            "bootstrap_block_length": skill.bootstrap_block_length,
            "baseline_trials": skill.baseline_trials,
            "baseline_seed": skill.baseline_seed,
            "baseline_eligible_times": [
                timestamp.isoformat() for timestamp in skill.baseline_eligible_times
            ],
            "periods": [
                {
                    "name": period.name,
                    "start_at": period.start_at.isoformat(),
                    "end_at": period.end_at.isoformat(),
                }
                for period in skill.periods
            ],
            "experiment": {
                "experiment_id": experiment.experiment_id,
                "strategy_version": experiment.strategy_version,
                "strategy_frozen_at": experiment.strategy_frozen_at.isoformat(),
                "evaluation_data_end": experiment.evaluation_data_end.isoformat(),
                "variant_count": experiment.variant_count,
                "parameters_reference": experiment.parameters_reference,
            },
            "same_bar_policy": "loss",
        }
    )


def _report_experiment(
    experiment: ExperimentDefinition,
    periods: tuple[EvaluationPeriod, ...],
) -> SignalExperimentDefinition:
    holdout = next((period for period in periods if period.name == "holdout"), None)
    return SignalExperimentDefinition(
        experiment_id=experiment.experiment_id,
        strategy_version=experiment.strategy_version,
        strategy_frozen_at=experiment.strategy_frozen_at,
        evaluation_data_end=experiment.evaluation_data_end,
        variant_count=experiment.variant_count,
        parameters_reference=experiment.parameters_reference,
        holdout_is_post_freeze=(
            experiment.strategy_frozen_at < holdout.start_at
            if holdout is not None
            else None
        ),
    )


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metric_observations(
    instrument: InstrumentId,
    series: OutcomeSeries,
    study: SignalEvaluationStudy,
    *,
    signal_reference: str,
    series_reference: str,
    configuration_reference: str,
    fixed_horizon_bars: int,
    max_holding_bars: int,
) -> tuple[Observation, ...]:
    first = series.points[0]
    last = series.points[-1]
    source = normalize_provider_kind(series.source)
    common = {
        "algorithm": DerivedAlgorithm.SIGNAL_OUTCOME_EVALUATION.value,
        "point_count": len(series.points),
        "start_at": first.observed_at.isoformat(),
        "end_at": last.observed_at.isoformat(),
        "series_ref": series_reference,
        "reference": signal_reference,
        "configuration_ref": configuration_reference,
        "input_provider_kind": source.value,
    }
    result: list[Observation] = []
    evaluation_rows: list[
        tuple[str, datetime, datetime, str, EvaluationSummary]
    ] = [
        (
            "full_history",
            first.observed_at,
            last.observed_at,
            f"fixed_{fixed_horizon_bars}_bars",
            study.fixed_horizon,
        ),
        (
            "full_history",
            first.observed_at,
            last.observed_at,
            f"triple_{max_holding_bars}_bars",
            study.triple_barrier,
        ),
    ]
    for period in study.periods:
        evaluation_rows.extend(
            (
                (
                    period.name,
                    period.start_at,
                    period.end_at,
                    f"fixed_{fixed_horizon_bars}_bars",
                    period.fixed_horizon,
                ),
                (
                    period.name,
                    period.start_at,
                    period.end_at,
                    f"triple_{max_holding_bars}_bars",
                    period.triple_barrier,
                ),
            )
        )
    for evaluation_period, period_start, period_end, window, summary in evaluation_rows:
        values: tuple[tuple[MetricKind, int | float | None], ...] = (
            (MetricKind.SIGNAL_EVENT_COUNT, summary.event_count),
            (
                MetricKind.SIGNAL_NON_OVERLAPPING_EVENT_COUNT,
                summary.non_overlapping_event_count,
            ),
            (MetricKind.SIGNAL_WIN_RATE, summary.win_rate),
            (
                MetricKind.SIGNAL_NON_OVERLAPPING_WIN_RATE_LOWER_95,
                summary.non_overlapping_win_rate_lower_95,
            ),
            (MetricKind.SIGNAL_REWARD_RISK_RATIO, summary.reward_risk_ratio),
            (MetricKind.SIGNAL_WIN_PAYOFF_PRODUCT, summary.win_payoff_product),
            (MetricKind.SIGNAL_BREAK_EVEN_WIN_RATE, summary.break_even_win_rate),
            (MetricKind.SIGNAL_EDGE_OVER_BREAK_EVEN, summary.edge_over_break_even),
            (MetricKind.SIGNAL_EXPECTED_VALUE, summary.expected_value),
            (MetricKind.SIGNAL_EXPECTED_R, summary.expected_r),
            (
                MetricKind.SIGNAL_NON_OVERLAPPING_EXPECTED_R,
                summary.non_overlapping_expected_r,
            ),
            (
                MetricKind.SIGNAL_NON_OVERLAPPING_EXPECTED_R_LOWER_95,
                summary.non_overlapping_expected_r_lower_95,
            ),
            (MetricKind.SIGNAL_BASELINE_EXPECTED_R, summary.baseline_expected_r),
            (MetricKind.SIGNAL_EXCESS_EXPECTED_R, summary.excess_expected_r),
            (MetricKind.SIGNAL_PROFIT_FACTOR, summary.profit_factor),
        )
        for metric, value in values:
            if value is None or not math.isfinite(value):
                continue
            result.append(
                Observation(
                    instrument=instrument,
                    metric=metric,
                    value=value,
                    source="derived",
                    observed_at=min(period_end, last.observed_at),
                    provenance={
                        **common,
                        "window": window,
                        "evaluation_period": evaluation_period,
                        "evaluation_start_at": period_start.isoformat(),
                        "evaluation_end_at": period_end.isoformat(),
                    },
                )
            )
    return tuple(result)
