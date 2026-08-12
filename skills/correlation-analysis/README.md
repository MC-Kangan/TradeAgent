# Correlation Analysis

`correlation-analysis` describes historical co-movement across 2–9 instruments
using aligned daily returns. It is a portfolio-scoped, price-only research skill
and emits no directional signal.

Quick start:

```bash
trade-research run-skill correlation-analysis BASKET \
  --portfolio-instrument US:SPY \
  --portfolio-instrument US:TLT \
  --lookback 120
```

The result contains a bounded correlation matrix, aligned-return count, asset
volatility statistics, and auditable references to every input series.

See [SKILL.md](SKILL.md) for the complete contract and limitations.
