"""Evaluate the ten article-listed strategy proxies on SPX Yahoo data."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any
from zoneinfo import ZoneInfo

from trade_research.domain import InstrumentId
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
    OutcomeSpecification,
    SignalInstruction,
    evaluate_signals,
)

SignalGenerator = Callable[[Sequence[PricePoint]], tuple[SignalInstruction, ...]]

DAILY_SPECIFICATION = OutcomeSpecification(
    change_kind="relative",
    fixed_horizon_bars=10,
    profit_target=0.03,
    stop_loss=0.02,
    max_holding_bars=20,
    entry_lag_bars=1,
    barrier_basis="high_low",
)
INTRADAY_SPECIFICATION = OutcomeSpecification(
    change_kind="relative",
    fixed_horizon_bars=2,
    profit_target=0.005,
    stop_loss=0.0035,
    max_holding_bars=4,
    entry_lag_bars=1,
    barrier_basis="high_low",
)


def main() -> None:
    instrument = InstrumentId(symbol="^GSPC", market="INDEX")
    daily = YahooPriceProvider(range_="10y", interval="1d").price_history(instrument)
    intraday = YahooPriceProvider(range_="60d", interval="30m").price_history(instrument)

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
            specification = DAILY_SPECIFICATION
            raw_instructions = daily_generators[definition.slug](prices)
            instructions = raw_instructions
        else:
            prices = intraday
            specification = INTRADAY_SPECIFICATION
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
                "latest_raw_signals": [
                    {
                        "observed_at": instruction.observed_at.isoformat(),
                        "direction": instruction.direction,
                    }
                    for instruction in raw_instructions[-5:]
                ],
                "fixed_horizon": _summary(study.fixed_horizon),
                "triple_barrier": _summary(study.triple_barrier),
            }
        )

    payload = {
        "instrument": instrument.model_dump(mode="json"),
        "data": {
            "daily": _coverage(daily, "yahoo", "1d"),
            "intraday": _coverage(intraday, "yahoo", "30m"),
        },
        "methodology": {
            "daily": _specification(DAILY_SPECIFICATION),
            "intraday": _specification(INTRADAY_SPECIFICATION),
            "signal_timing": "bar-close observation; entry on next observation",
            "intraday_filter": (
                "requires the complete entry lag and maximum outcome horizon to remain "
                "inside the same America/New_York session"
            ),
            "comparison_scope": (
                "common entry-signal outcome test; strategy-native sizing and exits are "
                "not simulated"
            ),
        },
        "strategies": rows,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def _same_session_eligible(
    instructions: Sequence[SignalInstruction],
    prices: Sequence[PricePoint],
    *,
    required_forward_bars: int,
) -> tuple[SignalInstruction, ...]:
    timezone = ZoneInfo("America/New_York")
    indexes = {point.observed_at: index for index, point in enumerate(prices)}
    result: list[SignalInstruction] = []
    for instruction in instructions:
        index = indexes[instruction.observed_at]
        end_index = index + required_forward_bars
        if end_index >= len(prices):
            continue
        signal_date = instruction.observed_at.astimezone(timezone).date()
        end_date = prices[end_index].observed_at.astimezone(timezone).date()
        if signal_date == end_date:
            result.append(instruction)
    return tuple(result)


def _outcomes(prices: Sequence[PricePoint]) -> tuple[OutcomePoint, ...]:
    return tuple(
        OutcomePoint(
            observed_at=point.observed_at,
            value=point.close,
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


def _specification(specification: OutcomeSpecification) -> dict[str, str | int | float]:
    return {
        "change_kind": specification.change_kind,
        "fixed_horizon_bars": specification.fixed_horizon_bars,
        "profit_target": specification.profit_target,
        "stop_loss": specification.stop_loss,
        "max_holding_bars": specification.max_holding_bars,
        "entry_lag_bars": specification.entry_lag_bars,
        "barrier_basis": specification.barrier_basis,
    }


def _summary(summary: Any) -> dict[str, int | float | None]:
    return {
        "event_count": summary.event_count,
        "non_overlapping_event_count": summary.non_overlapping_event_count,
        "skipped_event_count": summary.skipped_event_count,
        "win_count": summary.win_count,
        "loss_count": summary.loss_count,
        "breakeven_count": summary.breakeven_count,
        "win_rate": summary.win_rate,
        "non_overlapping_win_rate": summary.non_overlapping_win_rate,
        "win_rate_lower_95": summary.non_overlapping_win_rate_lower_95,
        "average_win": summary.average_win,
        "average_loss": summary.average_loss,
        "reward_risk_ratio": summary.reward_risk_ratio,
        "opportunity_score": summary.opportunity_score,
        "expected_change": summary.expected_change,
        "expectancy_r": summary.expectancy_r,
        "profit_factor": summary.profit_factor,
    }


if __name__ == "__main__":
    main()
