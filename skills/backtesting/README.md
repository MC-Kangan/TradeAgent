# Backtesting

This immutable analyst skill tests simple daily long-only ideas against standard
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
      {"observed_at": "2026-01-05T00:00:00Z", "action": "add_long"},
      {"observed_at": "2026-01-20T00:00:00Z", "action": "reduce_long"},
      {"observed_at": "2026-02-02T00:00:00Z", "action": "exit_long"}
    ]
  },
  "position_budget": 1000,
  "commission": 0.001,
  "tranche_fraction": 0.2,
  "deployment_cap_fraction": 0.8,
  "minimum_addition_bars": 1
}
```

Events must be timezone-aware, ordered, unique, aligned exactly to supplied
bars after normalization to UTC. `add_long` creates one fixed-notional
fractional lot when the cooldown, cash, and position-budget deployment checks pass.
`reduce_long` closes the oldest eligible lot, while `exit_long` closes every
eligible lot. A minimum holding period can defer either sell action. Final exits
may be omitted, in which case remaining lots are marked to the final close.

Built-in SMA, MACD, RSI, and Markov strategies add only on state transitions and
use `exit_long` when their thesis reverses. In particular, a persistent RSI
condition does not add a new lot on every bar. RSI backtests use the same shared
Wilder calculation as `technical-basic` and return the complete RSI curve plus
entry and exit threshold series for charting.

The output is research, not execution advice. Signals fill on the next bar's
open. Stops and targets are calculated from that actual fill and become active
for subsequent bars. All markets use the same fractional-share execution model,
so an asset price above the position budget does not by itself prevent a fill.
Positions still open at the end are marked to the final close and reported
separately from closed-trade statistics.
Every result retains its bounded strategy and cost assumptions plus a canonical
configuration hash. Signal quality, execution audit, and position performance
are reported separately from the same canonical event stream. No optimizer,
short selling, portfolio simulation, arbitrary Python, or interactive plot is exposed.
