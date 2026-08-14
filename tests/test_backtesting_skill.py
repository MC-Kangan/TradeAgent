"""Contract and numerical tests for the bounded backtesting skill."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from trade_research.application import ResearchApplication
from trade_research.domain import (
    AnalysisRequest,
    InlinePriceBar,
    InlinePriceSeries,
    InstrumentId,
    LimitationKind,
    ReportStatus,
    SignalKind,
)
from trade_research.engine import ResearchEngine
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.queue import JobQueue
from trade_research.reporting import ReportStore, render_json
from trade_research.skills import BacktestingSkill, SkillRegistry
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


def test_external_events_must_alternate() -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        BacktestingSkillParameters.model_validate(
            {
                "strategy": {
                    "kind": "external_signals",
                    "name": "vibe-test",
                    "events": [
                        {"observed_at": timestamp.isoformat(), "action": "enter_long"},
                        {"observed_at": (timestamp + timedelta(days=1)).isoformat(),
                         "action": "enter_long"},
                    ],
                }
            }
        )


def test_external_signal_fills_at_next_bar_open() -> None:
    points = _prices([100.0, 110.0, 120.0, 130.0, 125.0, 140.0])
    parameters = BacktestingSkillParameters.model_validate(
        {
            "cash": 10_000,
            "commission": 0,
            "position_size": 0.5,
            "strategy": {
                "kind": "external_signals",
                "name": "vibe-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(),
                     "action": "enter_long"},
                    {"observed_at": points[3].observed_at.isoformat(),
                     "action": "exit_long"},
                ],
            },
        }
    )
    configured = configure_skill(BacktestingSkill(), parameters.model_dump(mode="json"))
    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.status is ReportStatus.COMPLETE
    assert result.signal is SignalKind.NOT_ASSESSED
    assert result.presentation is not None
    assert result.presentation.template == "backtesting-v1"
    assert len(result.presentation.trades) == 1
    assert result.presentation.trades[0].entry_at == points[2].observed_at
    assert result.presentation.trades[0].entry_price == points[2].open
    assert result.presentation.trades[0].exit_at == points[4].observed_at
    assert result.presentation.trades[0].exit_price == points[4].open


def test_crypto_backtest_uses_fractional_units_at_realistic_prices() -> None:
    points = _prices([50_000.0, 51_000.0, 52_000.0, 53_000.0, 54_000.0], market="CRYPTO")
    configured = configure_skill(
        BacktestingSkill(),
        {
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "crypto-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "enter_long"}
                ],
            },
        },
    )

    result = configured.analyze(
        InstrumentId(symbol="BTC-USD", market="CRYPTO"), _providers(points)
    )

    assert result.status is ReportStatus.COMPLETE
    assert result.presentation is not None
    assert len(result.presentation.trades) == 1
    assert 0 < result.presentation.trades[0].size < 1


def test_report_preserves_reproducibility_assumptions() -> None:
    points = _prices([100.0, 99.0, 98.0, 101.0, 104.0, 106.0])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "cash": 25_000,
            "commission": 0.002,
            "spread": 0.001,
            "position_size": 0.75,
            "stop_loss_pct": 0.08,
            "take_profit_pct": 0.2,
            "strategy": {"kind": "sma_crossover", "fast_window": 2, "slow_window": 3},
        },
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.presentation is not None
    assumptions = result.presentation.assumptions
    assert assumptions.cash == 25_000
    assert assumptions.commission == 0.002
    assert assumptions.spread == 0.001
    assert assumptions.position_size == 0.75
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
                    {"observed_at": points[1].observed_at.isoformat(), "action": "enter_long"}
                ],
            },
        },
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.presentation is not None
    trade = result.presentation.trades[0]
    assert trade.entry_price == 150.0
    assert trade.exit_price == 135.0


def test_unexecuted_entry_signal_is_reported_partial() -> None:
    points = _prices([50_000.0, 51_000.0, 52_000.0, 53_000.0, 54_000.0])
    configured = configure_skill(
        BacktestingSkill(),
        {
            "commission": 0,
            "strategy": {
                "kind": "external_signals",
                "name": "expensive-equity-test",
                "events": [
                    {"observed_at": points[1].observed_at.isoformat(), "action": "enter_long"}
                ],
            },
        },
    )

    with pytest.warns(UserWarning):
        result = configured.analyze(
            InstrumentId(symbol="TEST", market="US"), _providers(points)
        )

    assert result.status is ReportStatus.PARTIAL
    assert LimitationKind.UNEXECUTED_SIGNALS in result.limitations
    assert result.presentation is not None
    assert not result.presentation.trades


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
                    {"observed_at": same_instant.isoformat(), "action": "enter_long"}
                ],
            },
        },
    )

    result = configured.analyze(InstrumentId(symbol="TEST", market="US"), _providers(points))

    assert result.status is ReportStatus.COMPLETE
    assert result.presentation is not None
    assert len(result.presentation.trades) == 1


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

    assert result.status is ReportStatus.COMPLETE
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
                        "action": "enter_long",
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
                            "action": "enter_long",
                        }
                    ],
                },
            }
        },
        price_series=(
            InlinePriceSeries(
                instrument=instrument,
                source="yahoo",
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
    assert presentation["template"] == "backtesting-v1"
    assert len(presentation["trades"]) == 1
    assert presentation["assumptions"]["cash"] == 10_000
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
                "events": [{"observed_at": event_at, "action": "enter_long"}],
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
    assert "enter_long" not in payload
    assert app.describe_skill("backtesting")["supported_asset_types"] == ["equity", "crypto"]
