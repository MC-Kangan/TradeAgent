from datetime import UTC, datetime, timedelta

from trade_research.providers import PricePoint
from trade_research.strategy_showcase import (
    EvaluationSettings,
    available_strategies,
    build_chart_rows,
    evaluate_strategy,
)


def _prices() -> tuple[PricePoint, ...]:
    values = (
        [100.0] * 60
        + [100.0 + index for index in range(1, 31)]
        + [130.0 - index for index in range(1, 51)]
        + [80.0 + index for index in range(1, 51)]
    )
    return tuple(
        PricePoint(
            observed_at=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index),
            open=value,
            high=value * 1.005,
            low=value * 0.995,
            close=value,
            volume=1.0,
            source="yahoo",
            provenance={"reference": f"bar-{index}"},
        )
        for index, value in enumerate(values)
    )


def test_available_strategies_have_entry_and_exit_explanations() -> None:
    strategies = available_strategies()

    assert "turtle" in {strategy.slug for strategy in strategies}
    assert "moving-average" in {strategy.slug for strategy in strategies}
    assert "martingale" not in {strategy.slug for strategy in strategies}
    assert all(strategy.entry_criteria for strategy in strategies)
    assert all(strategy.exit_criteria for strategy in strategies)


def test_evaluate_strategy_uses_existing_generator_and_evaluator() -> None:
    result = evaluate_strategy(
        _prices(),
        "moving-average",
        EvaluationSettings(
            fixed_horizon_bars=5,
            profit_target=0.03,
            stop_loss=0.02,
            max_holding_bars=10,
            bootstrap_samples=100,
        ),
    )

    assert result.instructions
    assert result.study is not None
    assert result.study.triple_barrier.event_count > 0
    assert {event.direction for event in result.study.triple_barrier.events} == {
        "long",
        "short",
    }


def test_chart_rows_label_evaluated_entries_and_exits() -> None:
    result = evaluate_strategy(
        _prices(),
        "moving-average",
        EvaluationSettings(
            fixed_horizon_bars=5,
            profit_target=0.03,
            stop_loss=0.02,
            max_holding_bars=10,
            bootstrap_samples=100,
        ),
    )

    rows = build_chart_rows(_prices(), result, max_bars=None)

    kinds = {row["kind"] for row in rows}
    assert {"price", "buy", "sell", "exit"}.issubset(kinds)
    assert all(row["timestamp"].endswith("+00:00") for row in rows)
