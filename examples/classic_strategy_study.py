"""Evaluate the ten article-listed strategy proxies on SPX Yahoo data."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trade_research.domain import InstrumentId, SignalEvent
from trade_research.providers import OutcomePoint, PricePoint, YahooPriceProvider
from trade_research.skills.classic_strategy_signals import (
    CLASSIC_STRATEGIES,
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
    evaluate_signals,
)

SignalGenerator = Callable[[Sequence[PricePoint]], tuple[SignalEvent, ...]]

DAILY_WARMUP_BARS = {
    "turtle": 20,
    "gap": 14,
    "oliver-kell-ema": 20,
    "escalator": 20,
    "checkmate": 59,
}
INTRADAY_WARMUP_BARS = {
    "dolphin": 19,
    "r-breaker": 13,
    "dual-thrust": 26,
    "fairy-four-price": 13,
}

DAILY_SPECIFICATION = OutcomeSpecification(
    change_kind="relative",
    fixed_horizon_bars=10,
    profit_target=0.03,
    stop_loss=0.02,
    max_holding_bars=20,
    entry_lag_bars=1,
    barrier_basis="high_low",
    baseline_trials=200,
)
INTRADAY_SPECIFICATION = OutcomeSpecification(
    change_kind="relative",
    fixed_horizon_bars=2,
    profit_target=0.005,
    stop_loss=0.0035,
    max_holding_bars=4,
    entry_lag_bars=1,
    barrier_basis="high_low",
    baseline_trials=200,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print compact full-history and holdout triple-barrier evidence",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write the canonical JSON artifact to this path instead of stdout",
    )
    args = parser.parse_args()
    instrument = InstrumentId(symbol="^GSPC", market="INDEX")
    daily = YahooPriceProvider(range_="10y", interval="1d").price_history(instrument)
    intraday = YahooPriceProvider(range_="60d", interval="30m").price_history(instrument)
    daily_specification = _with_periods(DAILY_SPECIFICATION, daily)
    intraday_specification = _with_periods(INTRADAY_SPECIFICATION, intraday)
    intraday_specification = replace(
        intraday_specification,
        baseline_eligible_times=_same_session_candidate_times(
            intraday,
            required_forward_bars=max(
                intraday_specification.fixed_horizon_bars,
                intraday_specification.max_holding_bars,
            )
            + intraday_specification.entry_lag_bars,
        ),
    )

    daily_generators: dict[str, SignalGenerator] = {
        "turtle": turtle_signals,
        "gap": gap_signals,
        "oliver-kell-ema": ema_momentum_signals,
        "escalator": escalator_signals,
        "checkmate": checkmate_signals,
    }
    intraday_generators: dict[str, SignalGenerator] = {
        "dolphin": dolphin_signals,
        "r-breaker": r_breaker_signals,
        "dual-thrust": dual_thrust_signals,
        "fairy-four-price": fairy_four_price_signals,
    }

    rows: list[dict[str, object]] = []
    for definition in CLASSIC_STRATEGIES:
        if not definition.signal_evaluable:
            rows.append(
                {
                    "number": definition.number,
                    "slug": definition.slug,
                    "name": definition.name,
                    "timeframe": definition.timeframe,
                    "status": "not-signal-evaluable",
                    "reason": (
                        "Martingale changes position size after a loss but does not define "
                        "an independent long/short entry signal; win-rate times reward/risk "
                        "would conceal its path-dependent ruin risk."
                    ),
                }
            )
            continue

        if definition.timeframe == "daily":
            prices = daily
            specification = replace(
                daily_specification,
                baseline_eligible_times=_candidate_times(
                    daily,
                    warmup_bars=DAILY_WARMUP_BARS[definition.slug],
                    required_forward_bars=max(
                        daily_specification.fixed_horizon_bars,
                        daily_specification.max_holding_bars,
                    )
                    + daily_specification.entry_lag_bars,
                ),
            )
            raw_instructions = daily_generators[definition.slug](prices)
            instructions = raw_instructions
        else:
            prices = intraday
            strategy_candidate_times = set(
                _candidate_times(
                    intraday,
                    warmup_bars=INTRADAY_WARMUP_BARS[definition.slug],
                    required_forward_bars=max(
                        intraday_specification.fixed_horizon_bars,
                        intraday_specification.max_holding_bars,
                    )
                    + intraday_specification.entry_lag_bars,
                )
            )
            specification = replace(
                intraday_specification,
                baseline_eligible_times=tuple(
                    timestamp
                    for timestamp in intraday_specification.baseline_eligible_times
                    if timestamp in strategy_candidate_times
                ),
            )
            raw_instructions = intraday_generators[definition.slug](prices)
            instructions = _same_session_eligible(
                raw_instructions,
                prices,
                required_forward_bars=max(
                    specification.fixed_horizon_bars,
                    specification.max_holding_bars,
                )
                + specification.entry_lag_bars,
            )

        study = evaluate_signals(_outcomes(prices), instructions, specification)
        rows.append(
            {
                "number": definition.number,
                "slug": definition.slug,
                "name": definition.name,
                "timeframe": definition.timeframe,
                "status": "evaluated",
                "raw_signal_count": len(raw_instructions),
                "evaluated_signal_count": len(instructions),
                "baseline_eligible_timestamp_count": len(
                    specification.baseline_eligible_times
                ),
                "latest_raw_signals": [
                    {
                        "observed_at": instruction.observed_at.isoformat(),
                        "direction": instruction.direction,
                    }
                    for instruction in raw_instructions[-5:]
                ],
                "fixed_horizon": _summary(study.fixed_horizon),
                "triple_barrier": _summary(study.triple_barrier),
                "periods": {
                    period.name: {
                        "fixed_horizon": _summary(period.fixed_horizon),
                        "triple_barrier": _summary(period.triple_barrier),
                    }
                    for period in study.periods
                },
            }
        )

    payload: dict[str, Any] = {
        "instrument": instrument.model_dump(mode="json"),
        "data": {
            "daily": _coverage(daily, "yahoo", "1d"),
            "intraday": _coverage(intraday, "yahoo", "30m"),
        },
        "methodology": {
            "daily": _specification(daily_specification),
            "intraday": _specification(intraday_specification),
            "signal_timing": "bar-close observation; entry on next observation",
            "intraday_filter": (
                "requires the complete entry lag and maximum outcome horizon to remain "
                "inside the same America/New_York session"
            ),
            "comparison_scope": (
                "common entry-signal outcome test; strategy-native sizing and exits are "
                "not simulated"
            ),
            "ranking_scope": (
                "expected R uses the declared ex-ante stop as one risk unit; holdout and "
                "matched-timestamp baseline evidence are reported separately"
            ),
        },
        "strategies": rows,
    }
    selected_payload = _compact_payload(payload) if args.summary else payload
    serialized = json.dumps(selected_payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
        print(f"wrote {args.output}")
        return
    print(serialized, end="")


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    metric_names = (
        "event_count",
        "non_overlapping_event_count",
        "win_rate",
        "reward_risk_ratio",
        "expected_r",
        "non_overlapping_expected_r",
        "non_overlapping_expected_r_lower_95",
        "non_overlapping_expected_r_upper_95",
        "bootstrap_positive_fraction",
        "bootstrap_block_length",
        "baseline_expected_r",
        "excess_expected_r",
        "profit_factor",
    )
    for strategy in payload["strategies"]:
        if strategy["status"] != "evaluated":
            rows.append(strategy)
            continue
        full = strategy["triple_barrier"]
        holdout = strategy["periods"]["holdout"]["triple_barrier"]
        rows.append(
            {
                "number": strategy["number"],
                "name": strategy["name"],
                "timeframe": strategy["timeframe"],
                "baseline_eligible_timestamp_count": strategy[
                    "baseline_eligible_timestamp_count"
                ],
                "full_history": {name: full[name] for name in metric_names},
                "holdout": {name: holdout[name] for name in metric_names},
            }
        )
    return {
        "schema": "spx-classic-strategy-evaluation-v2",
        "instrument": payload["instrument"],
        "data": payload["data"],
        "methodology": payload["methodology"],
        "strategies": rows,
    }


def _same_session_eligible(
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
    result = []
    for index, point in enumerate(prices):
        end_index = index + required_forward_bars
        if end_index >= len(prices):
            continue
        signal_date = point.observed_at.astimezone(timezone).date()
        end_date = prices[end_index].observed_at.astimezone(timezone).date()
        if signal_date == end_date:
            result.append(point.observed_at)
    return tuple(result)


def _candidate_times(
    prices: Sequence[PricePoint],
    *,
    warmup_bars: int,
    required_forward_bars: int,
) -> tuple[datetime, ...]:
    final_index = len(prices) - required_forward_bars
    if final_index <= warmup_bars:
        return ()
    return tuple(
        point.observed_at
        for point in prices[warmup_bars:final_index]
    )


def _with_periods(
    specification: OutcomeSpecification,
    prices: Sequence[PricePoint],
) -> OutcomeSpecification:
    split_index = max(1, int(len(prices) * 0.8))
    return replace(
        specification,
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
    )


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


def _coverage(
    prices: Sequence[PricePoint], provider: str, interval: str
) -> dict[str, str | int]:
    return {
        "provider": provider,
        "interval": interval,
        "bars": len(prices),
        "start": prices[0].observed_at.isoformat(),
        "end": prices[-1].observed_at.isoformat(),
    }


def _specification(specification: OutcomeSpecification) -> dict[str, Any]:
    return {
        "change_kind": specification.change_kind,
        "fixed_horizon_bars": specification.fixed_horizon_bars,
        "profit_target": specification.profit_target,
        "stop_loss": specification.stop_loss,
        "max_holding_bars": specification.max_holding_bars,
        "entry_lag_bars": specification.entry_lag_bars,
        "barrier_basis": specification.barrier_basis,
        "bootstrap_samples": specification.bootstrap_samples,
        "bootstrap_block_length": specification.bootstrap_block_length,
        "baseline_trials": specification.baseline_trials,
        "periods": [
            {
                "name": period.name,
                "start": period.start_at.isoformat(),
                "end": period.end_at.isoformat(),
            }
            for period in specification.periods
        ],
    }


def _summary(summary: Any) -> dict[str, int | float | None]:
    return {
        "event_count": summary.event_count,
        "non_overlapping_event_count": summary.non_overlapping_event_count,
        "skipped_event_count": summary.skipped_event_count,
        "purged_event_count": summary.purged_event_count,
        "win_count": summary.win_count,
        "loss_count": summary.loss_count,
        "breakeven_count": summary.breakeven_count,
        "win_rate": summary.win_rate,
        "non_overlapping_win_rate": summary.non_overlapping_win_rate,
        "win_rate_lower_95": summary.non_overlapping_win_rate_lower_95,
        "average_win": summary.average_win,
        "average_loss": summary.average_loss,
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
