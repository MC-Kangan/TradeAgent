# Backtesting

This immutable analyst skill tests simple daily long/flat ideas against standard
OHLC price bars. It wraps `backtesting.py` 0.6.6 behind Trade Research's existing
provider, provenance, report, HTTP, CLI, and MCP boundaries.

Select `backtesting` and provide parameters under
`skill_parameters.backtesting`. The default strategy is a 20/50 SMA crossover.
Available strategy kinds are `sma_crossover`, `macd_crossover`,
`rsi_mean_reversion`, `markov_regime`, and `external_signals`.
`start_date` sets the performance boundary while preserving earlier causal
warm-up bars, and `minimum_holding_bars` defaults to one daily bar.

An external Vibe Research idea looks like:

```json
{
  "strategy": {
    "kind": "external_signals",
    "name": "vibe-momentum-test",
    "events": [
      {"observed_at": "2026-01-05T00:00:00Z", "action": "enter_long"},
      {"observed_at": "2026-02-02T00:00:00Z", "action": "exit_long"}
    ]
  },
  "cash": 10000,
  "commission": 0.001,
  "position_size": 0.95
}
```

Events must be timezone-aware, ordered, unique, aligned exactly to supplied
bars after normalization to UTC, and alternate starting with `enter_long`. The final exit may be omitted;
the engine then reports the position open and marked to the final close.

The output is research, not execution advice. Signals fill on the next bar's
open. Stops and targets are calculated from that actual fill and become active
for subsequent bars. Crypto uses fractional units; equity simulations retain
whole-share behavior and explicitly report entry signals that could not execute.
Positions still open at the end are marked to the final close and reported
separately from closed-trade statistics.
Every result retains its bounded strategy and cost assumptions plus a canonical
configuration hash. No optimizer, short selling, portfolio simulation, arbitrary Python, or
interactive plot is exposed.
