"""Source-independent historical outcome evaluation for timestamped signals."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
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
    SignalEvaluationPresentation,
    SignalEvaluationSummary,
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


@dataclass(frozen=True, slots=True)
class SignalInstruction:
    """One deterministic instruction emitted without knowledge of future outcomes."""

    observed_at: datetime
    direction: Direction

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("signal timestamps must include a timezone")
        if self.direction not in {"long", "short"}:
            raise ValueError("signal direction must be long or short")


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
    reward_risk_ratio: float | None
    opportunity_score: float | None
    expected_change: float | None
    expectancy_r: float | None
    profit_factor: float | None


@dataclass(frozen=True, slots=True)
class SignalEvaluationStudy:
    """Fixed-horizon and path-dependent views of the same signal stream."""

    fixed_horizon: EvaluationSummary
    triple_barrier: EvaluationSummary


def evaluate_signals(
    points: tuple[OutcomePoint, ...],
    instructions: tuple[SignalInstruction, ...],
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

    return SignalEvaluationStudy(
        fixed_horizon=_summarize(
            tuple(fixed_events), fixed_skipped, specification.fixed_horizon_bars
        ),
        triple_barrier=_summarize(
            tuple(barrier_events), barrier_skipped, specification.max_holding_bars
        ),
    )


def _validate_inputs(
    points: tuple[OutcomePoint, ...],
    instructions: tuple[SignalInstruction, ...],
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
    if specification.change_kind == "relative" and any(
        point.lower <= 0 for point in points
    ):
        raise ValueError("relative outcome evaluation requires positive values")


def _fixed_outcome(
    points: tuple[OutcomePoint, ...],
    instruction: SignalInstruction,
    entry_index: int,
    exit_index: int,
    change_kind: ChangeKind,
) -> SignalOutcome:
    entry = points[entry_index]
    exit_point = points[exit_index]
    change = _directional_change(
        entry.value, exit_point.value, instruction.direction, change_kind
    )
    return SignalOutcome(
        signal_at=instruction.observed_at,
        entry_at=entry.observed_at,
        exit_at=exit_point.observed_at,
        direction=instruction.direction,
        entry_value=entry.value,
        exit_value=exit_point.value,
        change=change,
        duration_bars=exit_index - entry_index,
        exit_reason="fixed_horizon",
        same_bar_ambiguous=False,
        entry_index=entry_index,
        exit_index=exit_index,
    )


def _barrier_outcome(
    points: tuple[OutcomePoint, ...],
    instruction: SignalInstruction,
    entry_index: int,
    end_index: int,
    specification: OutcomeSpecification,
    *,
    horizon_complete: bool,
) -> SignalOutcome | None:
    entry = points[entry_index]
    for exit_index in range(entry_index + 1, end_index + 1):
        point = points[exit_index]
        if specification.barrier_basis == "high_low":
            favorable_value = point.upper if instruction.direction == "long" else point.lower
            adverse_value = point.lower if instruction.direction == "long" else point.upper
        else:
            favorable_value = point.value
            adverse_value = point.value
        favorable = _directional_change(
            entry.value,
            favorable_value,
            instruction.direction,
            specification.change_kind,
        )
        adverse = _directional_change(
            entry.value,
            adverse_value,
            instruction.direction,
            specification.change_kind,
        )
        hit_target = favorable >= specification.profit_target
        hit_stop = adverse <= -specification.stop_loss
        if not hit_target and not hit_stop:
            continue
        # Daily and snapshot data cannot reveal which barrier traded first.
        # Scoring the loss is deterministic and avoids optimistic path assumptions.
        is_loss = hit_stop
        change = -specification.stop_loss if is_loss else specification.profit_target
        exit_value = _barrier_value(
            entry.value,
            change,
            instruction.direction,
            specification.change_kind,
        )
        return SignalOutcome(
            signal_at=instruction.observed_at,
            entry_at=entry.observed_at,
            exit_at=point.observed_at,
            direction=instruction.direction,
            entry_value=entry.value,
            exit_value=exit_value,
            change=change,
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
        entry.value,
        exit_point.value,
        instruction.direction,
        specification.change_kind,
    )
    return SignalOutcome(
        signal_at=instruction.observed_at,
        entry_at=entry.observed_at,
        exit_at=exit_point.observed_at,
        direction=instruction.direction,
        entry_value=entry.value,
        exit_value=exit_point.value,
        change=change,
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


def _summarize(
    events: tuple[SignalOutcome, ...], skipped_event_count: int, embargo_bars: int
) -> EvaluationSummary:
    changes = [event.change for event in events]
    wins = [change for change in changes if change > 0]
    losses = [change for change in changes if change < 0]
    breakeven_count = len(changes) - len(wins) - len(losses)
    non_overlapping = _non_overlapping_events(events, embargo_bars)
    non_overlapping_wins = sum(event.change > 0 for event in non_overlapping)
    win_rate = len(wins) / len(changes) if changes else None
    non_overlapping_win_rate = (
        non_overlapping_wins / len(non_overlapping) if non_overlapping else None
    )
    average_win = statistics.fmean(wins) if wins else None
    average_loss = abs(statistics.fmean(losses)) if losses else None
    reward_risk = None
    expectancy_r = None
    if average_loss is not None and average_loss != 0:
        if average_win is not None:
            reward_risk = average_win / average_loss
        if changes:
            expectancy_r = (sum(wins) + sum(losses)) / len(changes) / average_loss
    loss_sum = abs(sum(losses))
    profit_factor = sum(wins) / loss_sum if wins and loss_sum > 0 else None
    return EvaluationSummary(
        events=events,
        skipped_event_count=skipped_event_count,
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
        reward_risk_ratio=reward_risk,
        opportunity_score=(
            win_rate * reward_risk
            if win_rate is not None and reward_risk is not None
            else None
        ),
        expected_change=statistics.fmean(changes) if changes else None,
        expectancy_r=expectancy_r,
        profit_factor=profit_factor,
    )


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
    instructions: tuple[SignalInstruction, ...] = ()
    change_kind: ChangeKind = "relative"
    fixed_horizon_bars: int = 21
    profit_target: float = 0.06
    stop_loss: float = 0.03
    max_holding_bars: int = 63
    entry_lag_bars: int = 1
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
        specification = OutcomeSpecification(
            change_kind=self.change_kind,
            fixed_horizon_bars=self.fixed_horizon_bars,
            profit_target=self.profit_target,
            stop_loss=self.stop_loss,
            max_holding_bars=self.max_holding_bars,
            entry_lag_bars=self.entry_lag_bars,
            barrier_basis=series.barrier_basis,
        )
        study = evaluate_signals(series.points, self.instructions, specification)
        signal_reference = _signal_reference(self.instructions)
        series_reference = _outcome_series_reference(series)
        configuration_reference = _configuration_reference(
            self, series.barrier_basis, signal_reference
        )
        presentation = SignalEvaluationPresentation(
            signal_name=self.signal_name,
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
        reward_risk_ratio=summary.reward_risk_ratio,
        opportunity_score=summary.opportunity_score,
        expected_change=summary.expected_change,
        expectancy_r=summary.expectancy_r,
        profit_factor=summary.profit_factor,
        events=tuple(
            ReportSignalOutcome(
                signal_at=event.signal_at,
                entry_at=event.entry_at,
                exit_at=event.exit_at,
                direction=event.direction,
                entry_value=event.entry_value,
                exit_value=event.exit_value,
                change=event.change,
                duration_bars=event.duration_bars,
                exit_reason=event.exit_reason,
                same_bar_ambiguous=event.same_bar_ambiguous,
            )
            for event in events
        ),
        presentation_reduced=reduced,
    )


def _signal_reference(instructions: tuple[SignalInstruction, ...]) -> str:
    rows = [
        {"observed_at": event.observed_at.isoformat(), "direction": event.direction}
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
            "same_bar_policy": "loss",
        }
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
    for window, summary in (
        (f"fixed_{fixed_horizon_bars}_bars", study.fixed_horizon),
        (f"triple_{max_holding_bars}_bars", study.triple_barrier),
    ):
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
            (MetricKind.SIGNAL_OPPORTUNITY_SCORE, summary.opportunity_score),
            (MetricKind.SIGNAL_EXPECTED_CHANGE, summary.expected_change),
            (MetricKind.SIGNAL_EXPECTANCY_R, summary.expectancy_r),
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
                    observed_at=last.observed_at,
                    provenance={**common, "window": window},
                )
            )
    return tuple(result)
