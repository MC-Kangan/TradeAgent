# Signal evaluation

`signal-evaluation` standardizes how Trade Research judges any deterministic,
timestamped trading idea. The signal producer and outcome series are separate:
an RSI/MA/IV model can be evaluated against a stock price series, while an IV
mean-reversion signal can be evaluated against a fixed-delta volatility series.

The first end-to-end adapter accepts the existing bounded daily price bars. Supply
parameters under `skill_parameters.signal-evaluation`:

```json
{
  "signal_name": "rsi-ma-iv",
  "target_series": {
    "name": "close",
    "kind": "price",
    "unit": "price",
    "strike_convention": "not_applicable"
  },
  "instructions": [
    {"observed_at": "2026-01-05T00:00:00Z", "direction": "long"},
    {"observed_at": "2026-02-03T00:00:00Z", "direction": "short"}
  ],
  "change_kind": "relative",
  "entry_lag_bars": 1,
  "fixed_horizon_bars": 21,
  "profit_target": 0.06,
  "stop_loss": 0.03,
  "max_holding_bars": 63
}
```

Use `change_kind: "absolute"` when evaluating an IV series expressed as decimals;
for example, a target of `0.02` means two volatility points when IV is `0.20`.
An implied-volatility target identifies either `floating_delta` with `call_delta`
and `tenor`, or `fixed_strike` with `strike` and `tenor`. The normalized series also
declares `observed_value` or `high_low` barrier semantics; mixed coverage is rejected.
Signal timestamps must be timezone-aware, ordered, unique, and exactly aligned to
the supplied outcome series. Entry defaults to the next observation.

Price providers are automatically exposed as a normalized `close` outcome series.
Dedicated Bloomberg or internal adapters implement the same bounded `OUTCOMES`
contract, so the analyst contains no vendor-specific retrieval logic.

This is an immediate-only research workflow because raw series and external signal
instructions are not stored in the durable job queue. See [SKILL.md](SKILL.md) for
the complete method and limitations.
