"""Contract and numerical tests for the bounded backtesting skill."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from trade_research.application import ResearchApplication
from trade_research.domain import (
    AnalysisRequest,
    InlinePriceBar,
    InlinePriceSeries,
    InstrumentId,
    ReportStatus,
    SignalEvent,
    SignalKind,
)
from trade_research.engine import ResearchEngine
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.queue import JobQueue
from trade_research.reporting import ReportStore, render_json
from trade_research.skills import BacktestingSkill, SkillRegistry
from trade_research.skills.backtesting import _curve_presentation_indexes
from trade_research.skills.parameters import BacktestingSkillParameters, configure_skill


def _prices(closes: list[float], *, market: str = "US") -> tuple[PricePoint, ...]:
    instrument = InstrumentId(
        symbol="BTC-USD" if market == "CRYPTO" else "TEST",
        market=market,
    )
    return tuple(
        PricePoint(
            instrument=instrument,
            observed_at=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index),
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=1_000.0 + index,
            source="fixture",
            provenance={
                "provider_kind": "fixture",
                "reference": f"sha256:{hashlib.sha256(str(index).encode()).hexdigest()}",
            },
        )
        for index, close in enumerate(closes)
    )


def _providers(points: tuple[PricePoint, ...]) -> ProviderRegistry:
    class Prices:
        def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
            return points

    return ProviderRegistry({CapabilityName.PRICES: Prices()})


def test_default_registry_exposes_backtesting() -> None:
    assert "backtesting" in ResearchEngine.from_settings().skills.names


def test_backtesting_parameters_publish_discriminated_strategy_schema() -> None:
    schema = BacktestingSkillParameters.model_json_schema()
    assert "strategy" in schema["properties"]
    assert BacktestingSkillParameters().strategy.kind == "sma_crossover"
    assert BacktestingSkillParameters().position_budget == 1_000
    assert BacktestingSkillParameters().tranche_fraction == 0.2
    assert BacktestingSkillParameters().deployment_cap_fraction == 0.8
    assert BacktestingSkillParameters().minimum_addition_bars == 1

    with pytest.raises(ValueError, match="tranche_fraction"):
        BacktestingSkillParameters.model_validate(
            {"tranche_fraction": 0.5, "deployment_cap_fraction": 0.4}
        )

    with pytest.raises(ValueError):
        BacktestingSkillParameters.model_validate({"cash": 1_000})


def test_external_events_allow_repeated_buy_and_sell_signals() -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    parameters = BacktestingSkillParameters.model_validate(
        {
            "strategy": {
                "kind": "external_signals",
                "name": "vibe-test",
                "events": [
                    {"observed_at": timestamp.isoformat(), "action": "add_long"},
                    {"observed_at": (timestamp + timedelta(days=1)).isoformat(),
                     "action": "add_long"},
                    {"observed_at": (timestamp + timedelta(days=2)).isoformat(),
                     "action": "reduce_long"},
                    {"observed_at": (timestamp + timedelta(days=3)).isoformat(),
                     "action": "exit_long"},
                ],
            }
        }
    )

    assert [event.action for event in parameters.strategy.events] == [
        "add_long", "add_long", "reduce_long", "exit_long",
    ]


def test_external_signal_fills_at_next_bar_open() -> None:
    points = _prices([100.0, 110.0, 120.0, 130.0, 125.0, 140.0])
    parameters = BacktestingSkillParameters.model_validate(
        {
            "position_budget": 10_000,
            "commission": 0,
            "tranche_fraction": 0.5,
            "strategy": {
                "kind": "external_signals",
                "name": "vibe-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(),
                     "action": "add_long"},
                    {"observed_at": points[3].observed_at.isoformat(),
                     "action": "exit_long"},
                ],
            },
        }
    )
    configured = configure_skill(BacktestingSkill(), parameters.model_dump(mode="json"))
    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.status is ReportStatus.PARTIAL
    assert result.signal is SignalKind.NOT_ASSESSED
    assert result.presentation is not None
    assert result.presentation.template == "backtesting-v2"
    assert len(result.presentation.trades) == 1
    assert result.presentation.trades[0].entry_at == points[2].observed_at
    assert result.presentation.trades[0].entry_price == pytest.approx(points[2].open)
    assert result.presentation.trades[0].exit_at == points[4].observed_at
    assert result.presentation.trades[0].exit_price == pytest.approx(points[4].open)


def test_signal_quality_uses_the_same_next_open_as_execution() -> None:
    original = _prices([100.0, 100.0, 120.0, 100.0, 100.0])
    points = (
        *original[:2],
        replace(original[2], open=80.0, high=121.0, low=79.0),
        *original[3:],
    )
    configured = configure_skill(
        BacktestingSkill(),
        {
            "commission": 0,
            "signal_horizon_bars": 1,
            "strategy": {
                "kind": "external_signals",
                "name": "gap-quality-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "add_long"}
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.presentation is not None
    assert result.presentation.open_positions[0].entry_price == 80.0
    assert result.presentation.signal_quality.expected_change == pytest.approx(0.25)
    assert result.presentation.signal_quality.status == "complete"


def test_backtest_and_signal_evaluator_share_the_domain_signal_event() -> None:
    points = _prices([100.0, 101.0, 102.0, 103.0])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "signal_horizon_bars": 1,
            "strategy": {
                "kind": "external_signals",
                "name": "canonical-event-test",
                "events": [
                    {"observed_at": points[0].observed_at.isoformat(), "action": "add_long"}
                ],
            },
        },
    )

    assert isinstance(configured.strategy.events[0], SignalEvent)
    assert configured.strategy.events[0].direction == "long"


def test_unevaluable_signal_quality_marks_the_integrated_report_partial() -> None:
    points = _prices([100.0, 101.0, 102.0, 103.0])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "signal_horizon_bars": 2,
            "strategy": {
                "kind": "external_signals",
                "name": "insufficient-forward-history",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "add_long"}
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert result.presentation.signal_quality.status == "insufficient_history"
    assert result.presentation.signal_quality.evaluated_signal_count == 0
    assert result.presentation.signal_quality.skipped_signal_count == 1
    assert "insufficient_history" in {item.value for item in result.limitations}


def test_one_signal_stream_drives_quality_execution_and_position_sections() -> None:
    points = _prices([100.0 + index for index in range(40)])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "position_budget": 1_000,
            "tranche_fraction": 0.2,
            "deployment_cap_fraction": 0.8,
            "signal_horizon_bars": 5,
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "integrated-signal-test",
                "events": [
                    {"observed_at": points[2].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[8].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[12].observed_at.isoformat(), "action": "reduce_long"},
                    {"observed_at": points[20].observed_at.isoformat(), "action": "exit_long"},
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.presentation is not None
    presentation = result.presentation
    assert presentation.signal_quality.source_add_signal_count == 2
    assert presentation.signal_quality.evaluated_signal_count == 2
    assert presentation.signal_quality.win_rate == 1
    assert presentation.signal_quality.expected_change is not None
    assert presentation.signal_quality.expected_change > 0
    assert presentation.execution_audit.add_signal_count == 2
    assert presentation.execution_audit.reduce_signal_count == 1
    assert presentation.execution_audit.exit_signal_count == 1
    assert presentation.execution_audit.executed_addition_count == 2
    assert presentation.position_performance.closed_lot_count == 2
    assert presentation.position_performance.final_equity > 1_000


def test_start_date_uses_prior_bars_only_for_warmup() -> None:
    points = _prices([100, 99, 98, 101, 104, 106, 103, 100, 105])
    start_date = points[3].observed_at.date()
    configured = configure_skill(
        BacktestingSkill(),
        {
            "start_date": start_date.isoformat(),
            "strategy": {"kind": "sma_crossover", "fast_window": 2, "slow_window": 3},
            "commission": 0,
        },
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.presentation is not None
    assert result.presentation.assumptions.start_date == start_date
    assert result.presentation.price_bars[0].observed_at == points[3].observed_at
    assert len(result.presentation.curve) == len(points) - 3
    assert all(
        trade.entry_at >= points[3].observed_at
        for trade in result.presentation.trades
    )


def test_long_history_uses_all_calculation_bars_but_bounds_chart_payload() -> None:
    points = _prices([100.0 + index * 0.01 for index in range(2_000)])
    start_index = 200
    configured = configure_skill(
        BacktestingSkill(),
        {
            "start_date": points[start_index].observed_at.date().isoformat(),
            "commission": 0,
            "signal_horizon_bars": 21,
            "strategy": {
                "kind": "external_signals",
                "name": "long-history-test",
                "events": [
                    {
                        "observed_at": points[start_index + 10].observed_at.isoformat(),
                        "action": "add_long",
                    }
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.status is ReportStatus.COMPLETE
    assert result.presentation is not None
    quality = result.presentation.data_quality
    assert quality.source_bar_count == 2_000
    assert quality.valid_bar_count == 2_000
    assert quality.warmup_bar_count == start_index
    assert quality.calculation_bar_count == 1_800
    assert quality.presented_bar_count == 520
    assert len(result.presentation.price_bars) == 520
    assert result.presentation.presentation_reduced is True
    assert "bounded_input" not in {item.value for item in result.limitations}


def test_many_open_lots_are_aggregated_but_bounded_for_presentation() -> None:
    points = _prices([100.0] * 700)
    events = [
        {
            "observed_at": points[index].observed_at.isoformat(),
            "action": "add_long",
        }
        for index in range(600)
    ]
    configured = configure_skill(
        BacktestingSkill(),
        {
            "commission": 0,
            "tranche_fraction": 0.001,
            "deployment_cap_fraction": 0.6,
            "strategy": {
                "kind": "external_signals",
                "name": "many-open-lots",
                "events": events,
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.presentation is not None
    performance = result.presentation.position_performance
    assert performance.open_lot_count > 520
    assert (
        performance.open_lot_count
        == result.presentation.execution_audit.executed_addition_count
    )
    assert performance.open_total_size > 0
    assert performance.open_average_entry_price == pytest.approx(100.0)
    assert performance.open_unrealized_pnl == pytest.approx(0.0)
    assert len(result.presentation.open_positions) == 100
    assert result.presentation.presentation_reduced is True


def test_curve_sampling_retains_equity_high_and_maximum_drawdown() -> None:
    frame = pd.DataFrame(
        {
            "Equity": [1_000.0] * 1_000,
            "DrawdownPct": [0.0] * 1_000,
        }
    )
    frame.loc[333, "Equity"] = 2_000.0
    frame.loc[337, "DrawdownPct"] = 0.75

    indexes = _curve_presentation_indexes(list(frame.iterrows()), 520)

    assert len(indexes) <= 520
    assert 333 in indexes
    assert 337 in indexes


def test_inline_price_contract_accepts_ten_year_daily_history() -> None:
    points = _prices([100.0 + index * 0.01 for index in range(3_650)])
    series = InlinePriceSeries(
        instrument=InstrumentId(symbol="TEST", market="US"),
        source="yahoo",
        currency="USD",
        price_adjustment="split_dividend_adjusted",
        daily_boundary="exchange_local",
        bars=tuple(
            InlinePriceBar(
                observed_at=point.observed_at,
                open=point.open,
                high=point.high,
                low=point.low,
                close=point.close,
                volume=point.volume,
            )
            for point in points
        ),
    )

    assert len(series.bars) == 3_650


def test_early_exit_is_deferred_until_minimum_holding_period() -> None:
    points = _prices([100, 101, 102, 103, 104, 105, 106])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "minimum_holding_bars": 3,
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "minimum-hold-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[2].observed_at.isoformat(), "action": "exit_long"},
                ],
            },
        },
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.presentation is not None
    assert result.presentation.trades[0].entry_at == points[2].observed_at
    assert result.presentation.trades[0].exit_at == points[5].observed_at
    assert result.presentation.trades[0].duration_bars == 3


def test_backtest_returns_chart_ready_indicator_series() -> None:
    points = _prices([100, 99, 98, 101, 104, 106])
    configured = configure_skill(
        BacktestingSkill(),
        {"strategy": {"kind": "sma_crossover", "fast_window": 2, "slow_window": 3}},
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.presentation is not None
    assert [series.key for series in result.presentation.indicator_series] == [
        "sma_fast", "sma_slow"
    ]
    assert result.presentation.indicator_series[0].points[-1].observed_at == points[-1].observed_at


def test_persistent_rsi_condition_adds_only_on_threshold_transition() -> None:
    points = _prices([100.0 - index for index in range(24)])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "position_budget": 1_000,
            "commission": 0,
            "strategy": {
                "kind": "rsi_mean_reversion",
                "window": 3,
                "entry_threshold": 30,
                "exit_threshold": 70,
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert len(result.presentation.open_positions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["US", "CRYPTO"])
async def test_rsi_curve_matches_technical_skill_across_asset_classes(
    market: str,
) -> None:
    closes = [100.0 + (index % 9) * 1.5 - (index % 5) * 0.8 for index in range(90)]
    points = _prices(closes, market=market)
    instrument = points[0].instrument
    request = AnalysisRequest(
        instrument=instrument,
        analysts=("backtesting", "technical-basic"),
        skill_parameters={
            "backtesting": {
                "commission": 0,
                "strategy": {
                    "kind": "rsi_mean_reversion",
                    "window": 14,
                    "entry_threshold": 30,
                    "exit_threshold": 70,
                },
            }
        },
    )

    report = await ResearchEngine.from_settings(providers=_providers(points)).analyze(
        request
    )
    results = {result.analyst: result for result in report.results}
    presentation = results["backtesting"].presentation

    assert presentation is not None
    series = {item.key: item for item in presentation.indicator_series}
    assert set(series) == {"rsi", "rsi_entry", "rsi_exit"}
    assert series["rsi"].panel == "oscillator"
    assert {point.value for point in series["rsi_entry"].points} == {30.0}
    assert {point.value for point in series["rsi_exit"].points} == {70.0}
    technical_rsi = next(
        item.value
        for item in results["technical-basic"].observations
        if item.metric.value == "relative_strength_index_14"
    )
    assert series["rsi"].points[-1].value == pytest.approx(technical_rsi)


@pytest.mark.parametrize("market", ["US", "CRYPTO"])
def test_every_market_uses_fractional_units_at_prices_above_starting_cash(
    market: str,
) -> None:
    points = _prices([50_000.0, 51_000.0, 52_000.0, 53_000.0, 54_000.0], market="CRYPTO")
    if market == "US":
        points = _prices([50_000.0, 51_000.0, 52_000.0, 53_000.0, 54_000.0])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "position_budget": 1_000,
            "tranche_fraction": 0.2,
            "deployment_cap_fraction": 1,
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "crypto-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "add_long"}
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="BTC-USD" if market == "CRYPTO" else "TEST", market=market),
        _providers(points),
    )

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert len(result.presentation.open_positions) == 1
    assert 0 < result.presentation.open_positions[0].size < 1
    assert (
        result.presentation.open_positions[0].size
        * result.presentation.open_positions[0].entry_price
    ) == pytest.approx(200, abs=0.01)


def test_report_preserves_reproducibility_assumptions() -> None:
    points = _prices([100.0, 99.0, 98.0, 101.0, 104.0, 106.0])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "position_budget": 25_000,
            "commission": 0.002,
            "spread": 0.001,
            "tranche_fraction": 0.25,
            "deployment_cap_fraction": 0.75,
            "minimum_addition_bars": 4,
            "stop_loss_pct": 0.08,
            "take_profit_pct": 0.2,
            "strategy": {"kind": "sma_crossover", "fast_window": 2, "slow_window": 3},
        },
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.presentation is not None
    assumptions = result.presentation.assumptions
    assert assumptions.position_budget == 25_000
    assert assumptions.commission == 0.002
    assert assumptions.spread == 0.001
    assert assumptions.tranche_fraction == 0.25
    assert assumptions.deployment_cap_fraction == 0.75
    assert assumptions.minimum_addition_bars == 4
    assert assumptions.stop_loss_pct == 0.08
    assert assumptions.take_profit_pct == 0.2
    assert assumptions.execution == "signal_close_next_open"
    assert {(item.key, item.value) for item in assumptions.strategy_parameters} == {
        ("fast_window", 2),
        ("slow_window", 3),
    }
    assert assumptions.configuration_reference.startswith("sha256:")


def test_protective_levels_are_anchored_to_actual_entry_price() -> None:
    original = _prices([100.0, 100.0, 150.0, 140.0, 140.0])
    points = (
        *original[:3],
        replace(original[3], open=140.0, high=142.0, low=130.0, close=140.0),
        original[4],
    )
    configured = configure_skill(
        BacktestingSkill(),
        {
            "commission": 0,
            "stop_loss_pct": 0.1,
            "strategy": {
                "kind": "external_signals",
                "name": "gap-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "add_long"}
                ],
            },
        },
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.presentation is not None
    trade = result.presentation.trades[0]
    assert trade.entry_price == 150.0
    assert trade.exit_price == 135.0


def test_protective_exit_waits_for_minimum_holding_period() -> None:
    original = _prices([100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    points = (
        *original[:3],
        replace(original[3], low=80.0),
        original[4],
        replace(original[5], low=80.0),
        original[6],
    )
    configured = configure_skill(
        BacktestingSkill(),
        {
            "commission": 0,
            "minimum_holding_bars": 3,
            "stop_loss_pct": 0.1,
            "strategy": {
                "kind": "external_signals",
                "name": "minimum-hold-stop-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "add_long"}
                ],
            },
        },
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.presentation is not None
    trade = result.presentation.trades[0]
    assert trade.entry_at == points[2].observed_at
    assert trade.exit_at == points[5].observed_at
    assert trade.duration_bars == 3


def test_repeated_signals_add_and_remove_fixed_notional_tranches() -> None:
    points = _prices([100.0] * 8)
    configured = configure_skill(
        BacktestingSkill(),
        {
            "position_budget": 1_000,
            "tranche_fraction": 0.2,
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "tranche-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[2].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[3].observed_at.isoformat(), "action": "reduce_long"},
                    {"observed_at": points[4].observed_at.isoformat(), "action": "reduce_long"},
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert len(result.presentation.trades) == 2
    assert [trade.entry_at for trade in result.presentation.trades] == [
        points[2].observed_at, points[3].observed_at,
    ]
    assert [trade.exit_at for trade in result.presentation.trades] == [
        points[4].observed_at, points[5].observed_at,
    ]
    assert all(trade.size == pytest.approx(2.0) for trade in result.presentation.trades)
    assert not result.presentation.open_positions


def test_entry_is_skipped_when_a_full_tranche_is_not_available() -> None:
    points = _prices([100.0] * 9)
    configured = configure_skill(
        BacktestingSkill(),
        {
            "position_budget": 1_000,
            "tranche_fraction": 0.2,
            "deployment_cap_fraction": 1,
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "cash-limit-test",
                "events": [
                    {
                        "observed_at": points[index].observed_at.isoformat(),
                        "action": "add_long",
                    }
                    for index in range(1, 7)
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert len(result.presentation.open_positions) == 5
    assert all(
        position.size * position.entry_price == pytest.approx(200, abs=0.01)
        for position in result.presentation.open_positions
    )


def test_maximum_allocation_caps_additions() -> None:
    points = _prices([100.0] * 9)
    configured = configure_skill(
        BacktestingSkill(),
        {
            "position_budget": 1_000,
            "tranche_fraction": 0.2,
            "deployment_cap_fraction": 0.6,
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "allocation-cap-test",
                "events": [
                    {
                        "observed_at": points[index].observed_at.isoformat(),
                        "action": "add_long",
                    }
                    for index in range(1, 7)
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert len(result.presentation.open_positions) == 3


def test_deployment_cap_uses_open_lot_entry_cost_not_market_value() -> None:
    points = _prices([100.0, 100.0, 100.0, 200.0, 200.0, 200.0, 200.0])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "position_budget": 1_000,
            "tranche_fraction": 0.2,
            "deployment_cap_fraction": 0.4,
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "entry-cost-cap-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[3].observed_at.isoformat(), "action": "add_long"},
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.presentation is not None
    assert len(result.presentation.open_positions) == 2
    assert sum(
        item.size * item.entry_price for item in result.presentation.open_positions
    ) == pytest.approx(400, abs=0.01)


def test_exit_long_closes_all_open_tranches() -> None:
    points = _prices([100.0] * 9)
    configured = configure_skill(
        BacktestingSkill(),
        {
            "position_budget": 1_000,
            "tranche_fraction": 0.2,
            "deployment_cap_fraction": 0.8,
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "exit-all-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[2].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[3].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[4].observed_at.isoformat(), "action": "exit_long"},
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert len(result.presentation.trades) == 3
    assert {trade.exit_at for trade in result.presentation.trades} == {
        points[5].observed_at
    }
    assert not result.presentation.open_positions


def test_addition_cooldown_skips_near_duplicate_signals() -> None:
    points = _prices([100.0] * 9)
    configured = configure_skill(
        BacktestingSkill(),
        {
            "position_budget": 1_000,
            "tranche_fraction": 0.2,
            "deployment_cap_fraction": 0.8,
            "minimum_addition_bars": 3,
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "cooldown-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[2].observed_at.isoformat(), "action": "add_long"},
                    {"observed_at": points[4].observed_at.isoformat(), "action": "add_long"},
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="TEST", market="US"), _providers(points)
    )

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert [position.entry_at for position in result.presentation.open_positions] == [
        points[2].observed_at,
        points[5].observed_at,
    ]


def test_external_event_matches_same_instant_with_different_offset() -> None:
    points = _prices([100.0, 101.0, 102.0, 103.0])
    same_instant = points[1].observed_at.astimezone(timezone(timedelta(hours=-5)))
    configured = configure_skill(
        BacktestingSkill(),
        {
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "offset-test",
                "events": [
                    {"observed_at": same_instant.isoformat(), "action": "add_long"}
                ],
            },
        },
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert result.presentation.open_positions


def test_sma_backtest_produces_bounded_report_data() -> None:
    closes = [100.0, 99.0, 98.0, 97.0, 96.0, 98.0, 101.0, 104.0, 107.0,
              106.0, 103.0, 100.0, 97.0, 95.0, 98.0, 102.0, 106.0]
    points = _prices(closes)
    parameters = {
        "strategy": {"kind": "sma_crossover", "fast_window": 2, "slow_window": 4},
        "commission": 0,
    }
    configured = configure_skill(BacktestingSkill(), parameters)
    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.status is ReportStatus.PARTIAL
    assert result.presentation is not None
    assert len(result.presentation.curve) == len(points)
    assert 1 <= len(result.presentation.trades) <= 200
    assert {item.metric.value for item in result.observations} >= {
        "backtest_total_return",
        "backtest_buy_hold_return",
        "backtest_max_drawdown",
        "backtest_trade_count",
    }


@pytest.mark.parametrize(
    "strategy",
    [
        {"kind": "macd_crossover", "fast_window": 2, "slow_window": 4,
         "signal_window": 2},
        {"kind": "rsi_mean_reversion", "window": 2, "entry_threshold": 40,
         "exit_threshold": 60},
        {"kind": "markov_regime", "window": 2, "bull_threshold": 0.01,
         "bear_threshold": -0.01, "min_train": 50},
    ],
)
def test_each_built_in_strategy_runs_through_the_same_contract(
    strategy: dict[str, object],
) -> None:
    closes = [100.0 + (index % 12) * 2 - (index % 5) * 3 for index in range(80)]
    points = _prices(closes)
    configured = configure_skill(
        BacktestingSkill(), {"strategy": strategy, "commission": 0}
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.status in {ReportStatus.COMPLETE, ReportStatus.PARTIAL}
    assert result.presentation is not None
    assert result.presentation.strategy_kind == strategy["kind"]


def test_misaligned_external_event_returns_partial() -> None:
    points = _prices([100.0, 101.0, 102.0, 103.0])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "strategy": {
                "kind": "external_signals",
                "name": "vibe-test",
                "events": [
                    {
                        "observed_at": datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
                        "action": "add_long",
                    }
                ],
            }
        },
    )
    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))
    assert result.status is ReportStatus.PARTIAL
    assert not result.observations


def test_non_daily_series_is_rejected() -> None:
    points = tuple(
        PricePoint(
            **{
                **point.__dict__,
                "observed_at": point.observed_at + timedelta(days=index * 6),
            }
        )
        for index, point in enumerate(_prices([100.0, 101.0, 102.0, 103.0]))
    )
    configured = configure_skill(
        BacktestingSkill(),
        {"strategy": {"kind": "sma_crossover", "fast_window": 2, "slow_window": 3}},
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.status is ReportStatus.PARTIAL
    assert not result.observations


@pytest.mark.asyncio
async def test_vibe_request_uses_existing_run_skill_and_inline_price_flow(
    tmp_path: Path,
) -> None:
    points = _prices([100.0, 101.0, 103.0, 102.0, 105.0])
    instrument = InstrumentId(symbol="TEST", market="US")
    request = AnalysisRequest(
        instrument=instrument,
        analysts=("backtesting",),
        skill_parameters={
            "backtesting": {
                "commission": 0,
                "strategy": {
                    "kind": "external_signals",
                    "name": "vibe-test",
                    "events": [
                        {
                            "observed_at": points[1].observed_at.isoformat(),
                            "action": "add_long",
                        }
                    ],
                },
            }
        },
        price_series=(
            InlinePriceSeries(
                instrument=instrument,
                source="yahoo",
                currency="USD",
                price_adjustment="split_dividend_adjusted",
                daily_boundary="exchange_local",
                bars=tuple(
                    InlinePriceBar(
                        observed_at=point.observed_at,
                        open=point.open,
                        high=point.high,
                        low=point.low,
                        close=point.close,
                        volume=point.volume,
                    )
                    for point in points
                ),
            ),
        ),
    )
    app = ResearchApplication(
        ResearchEngine.from_settings(), ReportStore(tmp_path / "reports")
    )

    payload = await app.run_skill("backtesting", request)

    presentation = payload["results"][0]["presentation"]
    assert presentation["template"] == "backtesting-v2"
    assert presentation["trades"] == []
    assert presentation["open_positions"][0]["entry_at"] == "2025-01-03T00:00:00Z"
    assert presentation["assumptions"]["position_budget"] == 1_000
    assert presentation["assumptions"]["strategy_parameters"] == [
        {"key": "event_count", "value": 1}
    ]
    assert presentation["assumptions"]["configuration_reference"].startswith("sha256:")


@pytest.mark.asyncio
async def test_backtesting_is_rejected_by_queue_instead_of_silently_changed(
    tmp_path: Path,
) -> None:
    app = ResearchApplication(
        ResearchEngine.from_settings(),
        ReportStore(tmp_path / "reports"),
        JobQueue(tmp_path / "jobs.sqlite3"),
    )
    instrument = InstrumentId(symbol="TEST", market="US")
    request = AnalysisRequest(
        instrument=instrument,
        analysts=("backtesting",),
        price_series=(
            InlinePriceSeries(
                instrument=instrument,
                source="yahoo",
                currency="USD",
                price_adjustment="split_dividend_adjusted",
                daily_boundary="exchange_local",
                bars=tuple(
                    InlinePriceBar(
                        observed_at=point.observed_at,
                        open=point.open,
                        high=point.high,
                        low=point.low,
                        close=point.close,
                        volume=point.volume,
                    )
                    for point in _prices([100.0, 101.0, 102.0])
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="immediate-only"):
        await app.start_research(request)


def test_serialized_report_omits_raw_external_events(tmp_path: Path) -> None:
    points = _prices([100.0, 101.0, 103.0, 102.0, 105.0])
    event_at = points[1].observed_at.isoformat()
    configured = configure_skill(
        BacktestingSkill(),
        {
            "strategy": {
                "kind": "external_signals",
                "name": "vibe-private-idea",
                "events": [{"observed_at": event_at, "action": "add_long"}],
            }
        },
    )
    app = ResearchApplication(
        ResearchEngine(SkillRegistry((configured,)), _providers(points)),
        ReportStore(tmp_path / "reports"),
    )
    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))
    from uuid import uuid4

    from trade_research.domain import ResearchReport

    payload = render_json(
        ResearchReport(
            request_id=uuid4(),
            instrument=InstrumentId(symbol="TEST", market="US"),
            results=(result,),
            generated_at=datetime.now(UTC),
        )
    )
    assert event_at not in payload
    assert "add_long" not in payload
    assert app.describe_skill("backtesting")["supported_asset_types"] == ["equity", "crypto"]
