import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trade_research.application import ResearchApplication
from trade_research.domain import (
    AnalysisRequest,
    InlineOutcomePoint,
    InlineOutcomeSeries,
    InlinePriceBar,
    InlinePriceSeries,
    InstrumentId,
    MetricKind,
    OutcomeSeriesSpec,
    SignalEvent,
)
from trade_research.engine import ResearchEngine
from trade_research.providers import ProviderRegistry
from trade_research.providers.inline import InlinePriceProvider
from trade_research.reporting import ReportStore
from trade_research.skills import SkillRegistry
from trade_research.skills.parameters import configure_skill
from trade_research.skills.signal_evaluation import (
    EvaluationPeriod,
    ExperimentDefinition,
    OutcomePoint,
    OutcomeSpecification,
    SignalEvaluationSkill,
    evaluate_fixed_horizon_signals,
    evaluate_signals,
)


def SignalInstruction(
    *, observed_at: datetime, direction: str, initial_risk: float | None = None
) -> SignalEvent:
    """Keep individual test cases compact while exercising the canonical event."""

    return SignalEvent(
        observed_at=observed_at,
        action="add_long" if direction == "long" else "add_short",
        initial_risk=initial_risk,
    )


def _at(day: int) -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day)


def _points(values: list[float]) -> tuple[OutcomePoint, ...]:
    return tuple(
        OutcomePoint(observed_at=_at(index), value=value)
        for index, value in enumerate(values)
    )


def test_fixed_horizon_entry_study_requires_no_stop_or_target_assumption() -> None:
    study = evaluate_fixed_horizon_signals(
        _points([100, 101, 103, 106, 105]),
        (SignalInstruction(observed_at=_at(0), direction="long"),),
        horizon_bars=2,
    )

    assert study.skipped_event_count == 0
    assert len(study.outcomes) == 1
    assert study.outcomes[0].entry_at == _at(1)
    assert study.outcomes[0].exit_at == _at(3)
    assert study.outcomes[0].change == pytest.approx(106 / 101 - 1)


def test_fixed_horizon_evaluates_long_and_short_instructions() -> None:
    study = evaluate_signals(
        _points([100, 100, 120, 120, 120, 90]),
        (
            SignalInstruction(observed_at=_at(0), direction="long"),
            SignalInstruction(observed_at=_at(3), direction="short"),
        ),
        OutcomeSpecification(
            change_kind="relative",
            fixed_horizon_bars=1,
            profit_target=0.05,
            stop_loss=0.03,
            max_holding_bars=1,
        ),
    )

    assert [event.change for event in study.fixed_horizon.events] == pytest.approx([0.2, 0.25])
    assert study.fixed_horizon.win_rate == 1.0
    assert study.fixed_horizon.event_count == 2
    assert study.fixed_horizon.non_overlapping_event_count == 2


def test_triple_barrier_reports_payoff_and_ex_ante_r_expectancy() -> None:
    points = (
        OutcomePoint(observed_at=_at(0), value=100),
        OutcomePoint(observed_at=_at(1), value=100),
        OutcomePoint(observed_at=_at(2), value=102, high=106, low=99),
        OutcomePoint(observed_at=_at(3), value=102),
        OutcomePoint(observed_at=_at(4), value=100),
        OutcomePoint(observed_at=_at(5), value=99, high=101, low=96),
    )
    study = evaluate_signals(
        points,
        (
            SignalInstruction(observed_at=_at(0), direction="long", initial_risk=0.02),
            SignalInstruction(observed_at=_at(3), direction="long", initial_risk=0.02),
        ),
        OutcomeSpecification(
            change_kind="relative",
            fixed_horizon_bars=1,
            profit_target=0.05,
            stop_loss=0.03,
            max_holding_bars=1,
            barrier_basis="high_low",
        ),
    )

    summary = study.triple_barrier
    assert [event.exit_reason for event in summary.events] == ["profit_target", "stop_loss"]
    assert [event.change for event in summary.events] == pytest.approx([0.05, -0.02])
    assert summary.win_rate == 0.5
    assert summary.reward_risk_ratio == pytest.approx(2.5)
    assert summary.win_payoff_product == pytest.approx(1.25)
    assert summary.break_even_win_rate == pytest.approx(2 / 7)
    assert summary.edge_over_break_even == pytest.approx(3 / 14)
    assert summary.expected_value == pytest.approx(0.015)
    assert summary.expected_r == pytest.approx(0.75)
    assert summary.non_overlapping_expected_r == pytest.approx(0.75)
    assert summary.profit_factor == pytest.approx(2.5)
    assert [event.r_multiple for event in summary.events] == pytest.approx([2.5, -1.0])


def test_fixed_stop_is_the_default_ex_ante_risk_unit() -> None:
    study = evaluate_signals(
        _points([100, 100, 106]),
        (SignalInstruction(observed_at=_at(0), direction="long"),),
        OutcomeSpecification(
            change_kind="relative",
            fixed_horizon_bars=1,
            profit_target=0.05,
            stop_loss=0.03,
            max_holding_bars=1,
        ),
    )

    event = study.fixed_horizon.events[0]
    assert event.initial_risk == pytest.approx(0.03)
    assert event.r_multiple == pytest.approx(2.0)
    assert study.fixed_horizon.expected_r == pytest.approx(2.0)


def test_relative_risk_rejects_values_that_could_create_nonpositive_prices() -> None:
    with pytest.raises(ValueError, match="relative initial_risk"):
        evaluate_signals(
            _points([100, 100, 90]),
            (
                SignalInstruction(
                    observed_at=_at(0), direction="long", initial_risk=1.0
                ),
            ),
            OutcomeSpecification(
                change_kind="relative",
                fixed_horizon_bars=1,
                profit_target=0.05,
                stop_loss=0.03,
                max_holding_bars=1,
            ),
        )


def test_bootstrap_uses_non_overlapping_r_multiples_deterministically() -> None:
    specification = OutcomeSpecification(
        change_kind="relative",
        fixed_horizon_bars=1,
        profit_target=0.50,
        stop_loss=0.10,
        max_holding_bars=1,
        bootstrap_samples=500,
        bootstrap_seed=7,
    )
    instructions = tuple(
        SignalInstruction(observed_at=_at(index), direction="long")
        for index in (0, 2, 4, 6)
    )
    points = _points([100, 100, 110, 100, 90, 100, 120, 100, 80])

    first = evaluate_signals(points, instructions, specification).fixed_horizon
    second = evaluate_signals(points, instructions, specification).fixed_horizon

    assert (
        first.non_overlapping_expected_r_lower_95
        == second.non_overlapping_expected_r_lower_95
    )
    assert (
        first.non_overlapping_expected_r_upper_95
        == second.non_overlapping_expected_r_upper_95
    )
    assert first.bootstrap_positive_fraction == second.bootstrap_positive_fraction
    assert first.non_overlapping_expected_r_lower_95 is not None
    assert first.non_overlapping_expected_r_upper_95 is not None
    assert first.bootstrap_block_length == 2
    assert 0 <= first.bootstrap_positive_fraction <= 1  # type: ignore[operator]


def test_break_even_edge_accounts_for_breakeven_events() -> None:
    study = evaluate_signals(
        _points([100, 100, 101, 101, 101, 101, 100]),
        tuple(
            SignalInstruction(observed_at=_at(index), direction="long")
            for index in (0, 2, 4)
        ),
        OutcomeSpecification(
            change_kind="absolute",
            fixed_horizon_bars=1,
            profit_target=10,
            stop_loss=1,
            max_holding_bars=1,
        ),
    ).fixed_horizon

    assert [event.r_multiple for event in study.events] == [1, 0, -1]
    assert study.expected_r == 0
    assert study.break_even_win_rate == pytest.approx(1 / 3)
    assert study.edge_over_break_even == pytest.approx(0)


def test_chronological_periods_purge_outcomes_crossing_boundaries() -> None:
    study = evaluate_signals(
        _points([100, 100, 101, 102, 103, 104, 105, 106]),
        (
            SignalInstruction(observed_at=_at(0), direction="long"),
            SignalInstruction(observed_at=_at(2), direction="long"),
            SignalInstruction(observed_at=_at(4), direction="long"),
        ),
        OutcomeSpecification(
            change_kind="relative",
            fixed_horizon_bars=2,
            profit_target=0.50,
            stop_loss=0.10,
            max_holding_bars=2,
            baseline_trials=10,
            periods=(
                EvaluationPeriod(name="development", start_at=_at(0), end_at=_at(3)),
                EvaluationPeriod(name="holdout", start_at=_at(4), end_at=_at(7)),
            ),
        ),
    )

    assert [period.name for period in study.periods] == ["development", "holdout"]
    assert study.periods[0].fixed_horizon.event_count == 1
    assert study.periods[0].fixed_horizon.purged_event_count == 1
    assert study.periods[0].fixed_horizon.skipped_event_count == 0
    assert study.periods[1].fixed_horizon.event_count == 1
    assert study.periods[1].fixed_horizon.baseline_trial_count == 10
    assert study.periods[1].fixed_horizon.baseline_expected_r is not None


def test_matched_timestamp_baseline_is_reproducible() -> None:
    specification = OutcomeSpecification(
        change_kind="relative",
        fixed_horizon_bars=1,
        profit_target=0.50,
        stop_loss=0.10,
        max_holding_bars=1,
        baseline_trials=20,
        baseline_seed=11,
    )
    instructions = (
        SignalInstruction(observed_at=_at(0), direction="long"),
        SignalInstruction(observed_at=_at(3), direction="short"),
    )
    points = _points([100, 102, 101, 100, 98, 99, 100, 101])

    first = evaluate_signals(points, instructions, specification)
    second = evaluate_signals(points, instructions, specification)

    assert first.fixed_horizon.baseline_trial_count == 20
    assert first.fixed_horizon.baseline_expected_r == second.fixed_horizon.baseline_expected_r
    assert first.fixed_horizon.excess_expected_r == second.fixed_horizon.excess_expected_r


def test_matched_baseline_uses_declared_candidate_universe_and_view_horizon() -> None:
    points = _points([100, 100, 101, 100, 104, 100, 90])
    study = evaluate_signals(
        points,
        (SignalInstruction(observed_at=_at(0), direction="long"),),
        OutcomeSpecification(
            change_kind="absolute",
            fixed_horizon_bars=1,
            profit_target=10,
            stop_loss=1,
            max_holding_bars=4,
            baseline_trials=10,
            baseline_eligible_times=(_at(2),),
        ),
    )

    # Signal at index 2 enters at 3 and exits at 4 for +4R. It is a valid
    # fixed-horizon candidate even though four barrier bars are unavailable.
    assert study.fixed_horizon.baseline_trial_count == 10
    assert study.fixed_horizon.baseline_expected_r == pytest.approx(4)
    assert study.triple_barrier.baseline_trial_count == 0


def test_same_bar_target_and_stop_is_scored_as_loss() -> None:
    points = (
        OutcomePoint(observed_at=_at(0), value=100),
        OutcomePoint(observed_at=_at(1), value=100),
        OutcomePoint(observed_at=_at(2), value=100, high=106, low=96),
    )
    study = evaluate_signals(
        points,
        (SignalInstruction(observed_at=_at(0), direction="long"),),
        OutcomeSpecification(
            change_kind="relative",
            fixed_horizon_bars=1,
            profit_target=0.05,
            stop_loss=0.03,
            max_holding_bars=1,
            barrier_basis="high_low",
        ),
    )

    event = study.triple_barrier.events[0]
    assert event.exit_reason == "stop_loss"
    assert event.change == pytest.approx(-0.03)
    assert event.same_bar_ambiguous is True


def test_absolute_changes_support_implied_volatility_points() -> None:
    points = (
        OutcomePoint(observed_at=_at(0), value=0.20),
        OutcomePoint(observed_at=_at(1), value=0.20),
        OutcomePoint(observed_at=_at(2), value=0.23, high=0.235, low=0.205),
    )
    study = evaluate_signals(
        points,
        (SignalInstruction(observed_at=_at(0), direction="long"),),
        OutcomeSpecification(
            change_kind="absolute",
            fixed_horizon_bars=1,
            profit_target=0.02,
            stop_loss=0.01,
            max_holding_bars=1,
            barrier_basis="high_low",
        ),
    )

    assert study.fixed_horizon.events[0].change == pytest.approx(0.03)
    assert study.triple_barrier.events[0].change == pytest.approx(0.02)
    assert study.triple_barrier.events[0].exit_reason == "profit_target"


def test_overlapping_events_are_counted_but_not_treated_as_independent() -> None:
    study = evaluate_signals(
        _points([100, 101, 102, 103, 104, 105]),
        (
            SignalInstruction(observed_at=_at(0), direction="long"),
            SignalInstruction(observed_at=_at(1), direction="long"),
        ),
        OutcomeSpecification(
            change_kind="relative",
            fixed_horizon_bars=3,
            profit_target=0.50,
            stop_loss=0.50,
            max_holding_bars=3,
        ),
    )

    assert study.fixed_horizon.event_count == 2
    assert study.fixed_horizon.non_overlapping_event_count == 1


def test_incomplete_events_are_reported_as_skipped() -> None:
    study = evaluate_signals(
        _points([100, 101, 102]),
        (SignalInstruction(observed_at=_at(1), direction="long"),),
        OutcomeSpecification(
            change_kind="relative",
            fixed_horizon_bars=2,
            profit_target=0.05,
            stop_loss=0.03,
            max_holding_bars=2,
        ),
    )

    assert study.fixed_horizon.event_count == 0
    assert study.fixed_horizon.skipped_event_count == 1
    assert study.triple_barrier.event_count == 0
    assert study.triple_barrier.skipped_event_count == 1


def test_triple_barrier_retains_known_hit_when_time_horizon_is_right_censored() -> None:
    study = evaluate_signals(
        _points([100, 100, 106, 106]),
        (SignalInstruction(observed_at=_at(0), direction="long"),),
        OutcomeSpecification(
            change_kind="relative",
            fixed_horizon_bars=1,
            profit_target=0.05,
            stop_loss=0.03,
            max_holding_bars=5,
        ),
    )

    assert study.triple_barrier.event_count == 1
    assert study.triple_barrier.skipped_event_count == 0
    assert study.triple_barrier.events[0].exit_reason == "profit_target"


def test_non_overlapping_sample_uses_outcome_independent_embargo() -> None:
    study = evaluate_signals(
        _points([100, 100, 106, 106, 106, 106, 106, 106, 106, 106]),
        (
            SignalInstruction(observed_at=_at(0), direction="long"),
            SignalInstruction(observed_at=_at(3), direction="long"),
        ),
        OutcomeSpecification(
            change_kind="relative",
            fixed_horizon_bars=1,
            profit_target=0.05,
            stop_loss=0.03,
            max_holding_bars=5,
        ),
    )

    assert study.triple_barrier.event_count == 2
    assert study.triple_barrier.non_overlapping_event_count == 1


def test_skill_exposes_standard_metrics_and_bounded_event_details() -> None:
    instrument = InstrumentId(symbol="SPY", market="ETF")
    values = [100, 100, 106, 106, 100, 96]
    series = InlinePriceSeries(
        instrument=instrument,
        source="yahoo",
        currency="USD",
        price_adjustment="split_dividend_adjusted",
        daily_boundary="exchange_local",
        bars=tuple(
            InlinePriceBar(
                observed_at=_at(index),
                open=value,
                high=value,
                low=value,
                close=value,
                volume=1_000,
            )
            for index, value in enumerate(values)
        ),
    )
    skill = SignalEvaluationSkill(
        signal_name="composite-test",
        experiment=ExperimentDefinition(
            experiment_id="composite-test-001",
            strategy_version="1.0.0",
            strategy_frozen_at=_at(2),
            evaluation_data_end=_at(5),
            variant_count=3,
        ),
        instructions=(
            SignalInstruction(observed_at=_at(0), direction="long"),
            SignalInstruction(observed_at=_at(3), direction="long"),
        ),
        fixed_horizon_bars=1,
        profit_target=0.05,
        stop_loss=0.03,
        max_holding_bars=1,
    )

    result = skill.analyze(
        instrument,
        ProviderRegistry({"prices": InlinePriceProvider((series,))}),
    )

    assert result.status == "complete"
    assert result.signal == "not_assessed"
    assert result.presentation is not None
    assert result.presentation.template == "signal-evaluation-v2"
    assert result.presentation.evaluation_purpose == "outcome_expectancy"
    assert result.presentation.experiment.variant_count == 3
    assert result.presentation.experiment.holdout_is_post_freeze is None
    assert result.presentation.signal_name == "composite-test"
    assert result.presentation.barrier_basis == "high_low"
    assert result.presentation.fixed_horizon.event_count == 2
    assert result.presentation.triple_barrier.reward_risk_ratio == pytest.approx(5 / 3)
    assert MetricKind.SIGNAL_WIN_RATE in {item.metric for item in result.observations}
    assert MetricKind.SIGNAL_REWARD_RISK_RATIO in {
        item.metric for item in result.observations
    }
    assert MetricKind.SIGNAL_WIN_PAYOFF_PRODUCT in {
        item.metric for item in result.observations
    }
    assert MetricKind.SIGNAL_EXPECTED_R in {item.metric for item in result.observations}
    assert all(
        item.provenance.get("configuration_ref")
        == result.presentation.configuration_reference
        for item in result.observations
    )


def test_skill_parameters_build_an_immutable_external_signal_study() -> None:
    configured = configure_skill(
        SignalEvaluationSkill(),
        {
            "signal_name": "rsi-ma-iv",
            "instructions": [
                {
                    "observed_at": _at(0).isoformat(),
                    "direction": "long",
                    "initial_risk": 0.02,
                },
                {
                    "observed_at": _at(3).isoformat(),
                    "direction": "short",
                    "initial_risk": 0.01,
                },
            ],
            "experiment": {
                "experiment_id": "rsi-ma-iv-001",
                "strategy_version": "2.1.0",
                "strategy_frozen_at": _at(5).isoformat(),
                "evaluation_data_end": _at(10).isoformat(),
                "variant_count": 12,
            },
            "change_kind": "absolute",
            "fixed_horizon_bars": 5,
            "profit_target": 0.02,
            "stop_loss": 0.01,
            "max_holding_bars": 10,
        },
    )

    assert isinstance(configured, SignalEvaluationSkill)
    assert configured.signal_name == "rsi-ma-iv"
    assert configured.change_kind == "absolute"
    assert configured.instructions[1].direction == "short"
    assert configured.instructions[1].initial_risk == pytest.approx(0.01)
    assert configured.experiment is not None
    assert configured.experiment.variant_count == 12


def test_default_engine_registers_signal_evaluation() -> None:
    engine = ResearchEngine.from_settings(providers=ProviderRegistry({}))
    assert "signal-evaluation" in engine.skills.names


@pytest.mark.asyncio
async def test_application_evaluates_timestamped_signals_with_inline_series(
    tmp_path: Path,
) -> None:
    instrument = InstrumentId(symbol="SPY", market="ETF")
    values = [100, 100, 106, 106, 100, 96]
    series = InlinePriceSeries(
        instrument=instrument,
        source="yahoo",
        currency="USD",
        price_adjustment="split_dividend_adjusted",
        daily_boundary="exchange_local",
        bars=tuple(
            InlinePriceBar(
                observed_at=_at(index),
                open=value,
                high=value,
                low=value,
                close=value,
                volume=1_000,
            )
            for index, value in enumerate(values)
        ),
    )
    application = ResearchApplication(
        ResearchEngine(
            SkillRegistry((SignalEvaluationSkill(),)),
            ProviderRegistry({}),
        ),
        ReportStore(tmp_path / "reports"),
    )
    request = AnalysisRequest(
        instrument=instrument,
        analysts=("signal-evaluation",),
        price_series=(series,),
        skill_parameters={
            "signal-evaluation": {
                "signal_name": "rsi-ma-iv",
                "instructions": [
                    {"observed_at": _at(0).isoformat(), "direction": "long"},
                    {"observed_at": _at(3).isoformat(), "direction": "long"},
                ],
                "fixed_horizon_bars": 1,
                "profit_target": 0.05,
                "stop_loss": 0.03,
                "max_holding_bars": 1,
            }
        },
    )

    payload = await application.run_skill("signal-evaluation", request)

    presentation = payload["results"][0]["presentation"]
    assert presentation["template"] == "signal-evaluation-v2"
    assert presentation["signal_name"] == "rsi-ma-iv"
    assert presentation["triple_barrier"]["win_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_application_evaluates_inline_implied_volatility_series(
    tmp_path: Path,
) -> None:
    instrument = InstrumentId(symbol="SPY", market="ETF")
    target_series = OutcomeSeriesSpec(
        name="spy-50d-1m-iv",
        kind="implied_volatility",
        unit="decimal_volatility",
        strike_convention="floating_delta",
        call_delta=0.5,
        tenor="1m",
    )
    series = InlineOutcomeSeries(
        instrument=instrument,
        spec=target_series,
        source="local_parquet",
        barrier_basis="observed_value",
        points=tuple(
            InlineOutcomePoint(observed_at=_at(index), value=value)
            for index, value in enumerate([0.20, 0.20, 0.23])
        ),
    )
    application = ResearchApplication(
        ResearchEngine(
            SkillRegistry((SignalEvaluationSkill(),)),
            ProviderRegistry({}),
        ),
        ReportStore(tmp_path / "reports"),
    )
    request = AnalysisRequest(
        instrument=instrument,
        analysts=("signal-evaluation",),
        outcome_series=(series,),
        skill_parameters={
            "signal-evaluation": {
                "signal_name": "iv-mean-reversion",
                "target_series": target_series.model_dump(mode="json"),
                "instructions": [
                    {"observed_at": _at(0).isoformat(), "direction": "long"}
                ],
                "change_kind": "absolute",
                "fixed_horizon_bars": 1,
                "profit_target": 0.02,
                "stop_loss": 0.01,
                "max_holding_bars": 1,
            }
        },
    )

    payload = await application.run_skill("signal-evaluation", request)

    presentation = payload["results"][0]["presentation"]
    assert presentation["target_series"]["call_delta"] == pytest.approx(0.5)
    assert presentation["target_series"]["tenor"] == "1m"
    assert presentation["change_kind"] == "absolute"
    assert presentation["triple_barrier"]["win_rate"] == pytest.approx(1.0)


def test_configuration_reference_changes_with_outcome_rules() -> None:
    instrument = InstrumentId(symbol="SPY", market="ETF")
    series = InlinePriceSeries(
        instrument=instrument,
        source="yahoo",
        currency="USD",
        price_adjustment="split_dividend_adjusted",
        daily_boundary="exchange_local",
        bars=tuple(
            InlinePriceBar(observed_at=_at(index), close=value)
            for index, value in enumerate([100, 100, 106])
        ),
    )
    providers = ProviderRegistry({"prices": InlinePriceProvider((series,))})
    common = {
        "instructions": (SignalInstruction(observed_at=_at(0), direction="long"),),
        "fixed_horizon_bars": 1,
        "stop_loss": 0.03,
        "max_holding_bars": 1,
    }

    first = SignalEvaluationSkill(profit_target=0.05, **common).analyze(
        instrument, providers
    )
    second = SignalEvaluationSkill(profit_target=0.06, **common).analyze(
        instrument, providers
    )

    assert first.presentation is not None
    assert second.presentation is not None
    assert first.presentation.barrier_basis == "observed_value"
    assert (
        first.presentation.configuration_reference
        != second.presentation.configuration_reference
    )


def test_configuration_reference_covers_experiment_identity() -> None:
    instrument = InstrumentId(symbol="SPY", market="ETF")
    series = InlinePriceSeries(
        instrument=instrument,
        source="yahoo",
        currency="USD",
        price_adjustment="split_dividend_adjusted",
        daily_boundary="exchange_local",
        bars=tuple(
            InlinePriceBar(observed_at=_at(index), close=value)
            for index, value in enumerate([100, 100, 106])
        ),
    )
    providers = ProviderRegistry({"prices": InlinePriceProvider((series,))})
    common = {
        "instructions": (SignalInstruction(observed_at=_at(0), direction="long"),),
        "fixed_horizon_bars": 1,
        "profit_target": 0.05,
        "stop_loss": 0.03,
        "max_holding_bars": 1,
    }

    first = SignalEvaluationSkill(
        experiment=ExperimentDefinition(
            experiment_id="ma-001",
            strategy_version="1.0.0",
            strategy_frozen_at=_at(1),
            evaluation_data_end=_at(2),
            variant_count=1,
        ),
        **common,
    ).analyze(instrument, providers)
    second = SignalEvaluationSkill(
        experiment=ExperimentDefinition(
            experiment_id="ma-001",
            strategy_version="1.0.0",
            strategy_frozen_at=_at(1),
            evaluation_data_end=_at(2),
            variant_count=2,
        ),
        **common,
    ).analyze(instrument, providers)

    assert first.presentation is not None
    assert second.presentation is not None
    assert first.presentation.configuration_reference != second.presentation.configuration_reference


def test_experiment_evaluation_end_trims_later_outcomes_and_marks_holdout() -> None:
    instrument = InstrumentId(symbol="SPY", market="ETF")
    series = InlinePriceSeries(
        instrument=instrument,
        source="yahoo",
        currency="USD",
        price_adjustment="split_dividend_adjusted",
        daily_boundary="exchange_local",
        bars=tuple(
            InlinePriceBar(observed_at=_at(index), close=value)
            for index, value in enumerate([100, 100, 106, 106, 110])
        ),
    )
    skill = SignalEvaluationSkill(
        instructions=(SignalInstruction(observed_at=_at(0), direction="long"),),
        fixed_horizon_bars=1,
        profit_target=0.05,
        stop_loss=0.03,
        max_holding_bars=1,
        experiment=ExperimentDefinition(
            experiment_id="ma-001",
            strategy_version="1.0.0",
            strategy_frozen_at=_at(1),
            evaluation_data_end=_at(3),
        ),
        periods=(
            EvaluationPeriod(name="development", start_at=_at(0), end_at=_at(1)),
            EvaluationPeriod(name="holdout", start_at=_at(2), end_at=_at(3)),
        ),
    )

    result = skill.analyze(
        instrument,
        ProviderRegistry({"prices": InlinePriceProvider((series,))}),
    )

    assert result.presentation is not None
    assert result.presentation.experiment.evaluation_data_end == _at(3)
    assert result.presentation.experiment.holdout_is_post_freeze is True
    assert all(
        event.exit_at <= _at(3)
        for event in result.presentation.fixed_horizon.events
    )


def test_period_metrics_are_exported_as_standard_observations() -> None:
    instrument = InstrumentId(symbol="SPY", market="ETF")
    series = InlinePriceSeries(
        instrument=instrument,
        source="yahoo",
        currency="USD",
        price_adjustment="split_dividend_adjusted",
        daily_boundary="exchange_local",
        bars=tuple(
            InlinePriceBar(observed_at=_at(index), close=value)
            for index, value in enumerate([100, 100, 101, 101, 102, 102])
        ),
    )
    result = SignalEvaluationSkill(
        instructions=(
            SignalInstruction(observed_at=_at(0), direction="long"),
            SignalInstruction(observed_at=_at(3), direction="long"),
        ),
        fixed_horizon_bars=1,
        profit_target=0.5,
        stop_loss=0.1,
        max_holding_bars=1,
        periods=(
            EvaluationPeriod(name="development", start_at=_at(0), end_at=_at(2)),
            EvaluationPeriod(name="holdout", start_at=_at(3), end_at=_at(5)),
        ),
    ).analyze(instrument, ProviderRegistry({"prices": InlinePriceProvider((series,))}))

    periods = {item.provenance.get("evaluation_period") for item in result.observations}
    assert periods == {"full_history", "development", "holdout"}
    holdout_expected_r = [
        item
        for item in result.observations
        if item.metric == MetricKind.SIGNAL_EXPECTED_R
        and item.provenance.get("evaluation_period") == "holdout"
    ]
    assert len(holdout_expected_r) == 2


def test_fixed_strike_volatility_series_has_typed_coordinates() -> None:
    spec = OutcomeSeriesSpec(
        name="spy-4500-3m-iv",
        kind="implied_volatility",
        unit="decimal_volatility",
        strike_convention="fixed_strike",
        strike=4_500,
        tenor="3m",
    )

    assert spec.strike == 4_500
    assert spec.call_delta is None

    with pytest.raises(ValueError, match="no call_delta"):
        OutcomeSeriesSpec(
            name="spy-4500-3m-iv",
            kind="implied_volatility",
            unit="decimal_volatility",
            strike_convention="fixed_strike",
            strike=4_500,
            call_delta=0.5,
            tenor="3m",
        )


def test_inline_outcome_series_rejects_mixed_barrier_coverage() -> None:
    with pytest.raises(ValueError, match="high_low barrier basis"):
        InlineOutcomeSeries(
            instrument=InstrumentId(symbol="SPY", market="ETF"),
            spec=OutcomeSeriesSpec(
                name="spy-50d-1m-iv",
                kind="implied_volatility",
                unit="decimal_volatility",
                strike_convention="floating_delta",
                call_delta=0.5,
                tenor="1m",
            ),
            source="local_parquet",
            barrier_basis="high_low",
            points=(
                InlineOutcomePoint(
                    observed_at=_at(0), value=0.20, high=0.21, low=0.19
                ),
                InlineOutcomePoint(observed_at=_at(1), value=0.20),
            ),
        )


def test_spx_html_references_and_matches_canonical_evidence() -> None:
    repository = Path(__file__).resolve().parents[1]
    artifact_path = repository / "reports" / "spx_classic_strategy_evaluation.json"
    html = (repository / "reports" / "spx_classic_strategy_evaluation.html").read_text(
        encoding="utf-8"
    )
    artifact_bytes = artifact_path.read_bytes()
    artifact = json.loads(artifact_bytes)

    assert artifact["schema"] == "spx-classic-strategy-evaluation-v2"
    assert hashlib.sha256(artifact_bytes).hexdigest() in html
    for strategy in artifact["strategies"]:
        if strategy.get("status") == "not-signal-evaluable":
            continue
        for sample in (strategy["full_history"], strategy["holdout"]):
            lower = _report_number(sample["non_overlapping_expected_r_lower_95"])
            upper = _report_number(sample["non_overlapping_expected_r_upper_95"])
            assert f"{lower} to {upper}" in html
            assert f'{sample["bootstrap_positive_fraction"] * 100:.1f}%' in html
            for metric in (
                "expected_r",
                "non_overlapping_expected_r",
                "excess_expected_r",
            ):
                assert _report_number(sample[metric]) in html


def _report_number(value: float) -> str:
    if round(value, 3) == 0:
        return "0.000"
    formatted = f"{value:+.3f}"
    return formatted.replace("-", "−")
