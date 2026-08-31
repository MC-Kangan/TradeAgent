"""Fetch daily prices, create MA-cross instructions, and evaluate their outcomes."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any

from trade_research.domain import InstrumentId
from trade_research.providers import OutcomePoint, PricePoint, YahooPriceProvider
from trade_research.signals import moving_average_crossover
from trade_research.skills.signal_evaluation import (
    EvaluationPeriod,
    OutcomeSpecification,
    evaluate_signals,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="^GSPC")
    parser.add_argument("--market", default="INDEX")
    parser.add_argument("--fast", type=int, default=5)
    parser.add_argument("--slow", type=int, default=20)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--profit-target", type=float, default=0.02)
    parser.add_argument("--stop-loss", type=float, default=0.015)
    parser.add_argument("--max-holding", type=int, default=10)
    args = parser.parse_args()

    instrument = InstrumentId(symbol=args.symbol, market=args.market)
    prices = YahooPriceProvider().price_history(instrument)
    instructions = moving_average_crossover(
        prices,
        fast_window=args.fast,
        slow_window=args.slow,
    )
    split_index = max(1, int(len(prices) * 0.7))
    study = evaluate_signals(
        _outcomes(prices),
        instructions,
        OutcomeSpecification(
            change_kind="relative",
            fixed_horizon_bars=args.horizon,
            profit_target=args.profit_target,
            stop_loss=args.stop_loss,
            max_holding_bars=args.max_holding,
            entry_lag_bars=1,
            barrier_basis="high_low",
            baseline_trials=200,
            periods=(
                EvaluationPeriod(
                    name="development",
                    start_at=prices[0].observed_at,
                    end_at=prices[split_index - 1].observed_at,
                ),
                EvaluationPeriod(
                    name="holdout",
                    start_at=prices[split_index].observed_at,
                    end_at=prices[-1].observed_at,
                ),
            ),
        ),
    )
    payload = {
        "instrument": instrument.model_dump(mode="json"),
        "data": {
            "provider": "yahoo",
            "bars": len(prices),
            "start": prices[0].observed_at.isoformat(),
            "end": prices[-1].observed_at.isoformat(),
        },
        "signal": {
            "name": f"sma-{args.fast}-{args.slow}-crossover",
            "count": len(instructions),
            "entry_lag_bars": 1,
            "evaluation_purpose": "outcome_expectancy",
        },
        "fixed_horizon": _summary(study.fixed_horizon),
        "triple_barrier": _summary(study.triple_barrier),
        "periods": [
            {
                "name": period.name,
                "start": period.start_at.isoformat(),
                "end": period.end_at.isoformat(),
                "fixed_horizon": _summary(period.fixed_horizon),
                "triple_barrier": _summary(period.triple_barrier),
            }
            for period in study.periods
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


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


def _summary(summary: Any) -> dict[str, int | float | None]:
    return {
        "event_count": summary.event_count,
        "non_overlapping_event_count": summary.non_overlapping_event_count,
        "skipped_event_count": summary.skipped_event_count,
        "win_rate": summary.win_rate,
        "win_rate_lower_95": summary.non_overlapping_win_rate_lower_95,
        "reward_risk_ratio": summary.reward_risk_ratio,
        "win_payoff_product": summary.win_payoff_product,
        "break_even_win_rate": summary.break_even_win_rate,
        "edge_over_break_even": summary.edge_over_break_even,
        "expected_value": summary.expected_value,
        "expected_r": summary.expected_r,
        "non_overlapping_expected_r": summary.non_overlapping_expected_r,
        "non_overlapping_expected_r_lower_95": (
            summary.non_overlapping_expected_r_lower_95
        ),
        "non_overlapping_expected_r_upper_95": (
            summary.non_overlapping_expected_r_upper_95
        ),
        "bootstrap_positive_fraction": summary.bootstrap_positive_fraction,
        "bootstrap_block_length": summary.bootstrap_block_length,
        "baseline_expected_r": summary.baseline_expected_r,
        "excess_expected_r": summary.excess_expected_r,
        "profit_factor": summary.profit_factor,
    }


if __name__ == "__main__":
    main()
