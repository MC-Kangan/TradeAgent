# worth-buy-stocks

Trend-scoring skill for the TradeAgent research framework.

A 4-layer pipeline that scores US equities on momentum, relative strength (vs
SPY/QQQ), risk factors, and technical confirmation, producing a trading
discipline verdict with entry/stop/target price levels.

## Original Algorithm

https://github.com/starriv/worth-buy-stocks

## Quick Start

```sh
# Requires a PRICES provider (e.g., Yahoo)
export TRADE_RESEARCH_PRICE_PROVIDER=yahoo

# Run on any US symbol
.venv/bin/trade-research run-skill worth-buy-stocks AAPL

# Benchmark symbols are configurable at construction
```

## Output

Returns an `AnalystResult` with:
- **Verdict**: 是 (buy) / 观察 (watch) / 否 (no) / 持仓需减风险 (reduce risk)
- **ALPHA composite score**: 0–100
- **Risk score**: penalties from MA structure, drawdown, weekly alignment
- **Entry timing**: price zones and classification
- **20 observations** with full provenance

## Configuration

See [SKILL.md](SKILL.md) for the full algorithm specification, layer formulas,
metrics table, and failure modes.
