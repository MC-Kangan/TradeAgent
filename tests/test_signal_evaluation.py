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
)
from trade_research.engine import ResearchEngine
from trade_research.providers import ProviderRegistry
from trade_research.providers.inline import InlinePriceProvider
from trade_research.reporting import ReportStore
from trade_research.skills import SkillRegistry
from trade_research.skills.parameters import configure_skill
from trade_research.skills.signal_evaluation import (
    OutcomePoint,
    OutcomeSpecification,
    SignalEvaluationSkill,
    SignalInstruction,
    evaluate_signals,
)


def _at(day: int) -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day)


def _points(values: list[float]) -> tuple[OutcomePoint, ...]:
    return tuple(
        OutcomePoint(observed_at=_at(index), value=value)
        for index, value in enumerate(values)
    )


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


def test_triple_barrier_reports_win_rate_reward_risk_and_expectancy_r() -> None:
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
            SignalInstruction(observed_at=_at(0), direction="long"),
            SignalInstruction(observed_at=_at(3), direction="long"),
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
    assert [event.change for event in summary.events] == pytest.approx([0.05, -0.03])
    assert summary.win_rate == 0.5
    assert summary.reward_risk_ratio == pytest.approx(5 / 3)
    assert summary.opportunity_score == pytest.approx(5 / 6)
    assert summary.expectancy_r == pytest.approx(1 / 3)
    assert summary.profit_factor == pytest.approx(5 / 3)


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
    assert result.presentation.template == "signal-evaluation-v1"
    assert result.presentation.signal_name == "composite-test"
    assert result.presentation.barrier_basis == "high_low"
    assert result.presentation.fixed_horizon.event_count == 2
    assert result.presentation.triple_barrier.reward_risk_ratio == pytest.approx(5 / 3)
    assert MetricKind.SIGNAL_WIN_RATE in {item.metric for item in result.observations}
    assert MetricKind.SIGNAL_REWARD_RISK_RATIO in {
        item.metric for item in result.observations
    }
    assert MetricKind.SIGNAL_OPPORTUNITY_SCORE in {
        item.metric for item in result.observations
    }
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
                {"observed_at": _at(0).isoformat(), "direction": "long"},
                {"observed_at": _at(3).isoformat(), "direction": "short"},
            ],
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
    assert presentation["template"] == "signal-evaluation-v1"
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
