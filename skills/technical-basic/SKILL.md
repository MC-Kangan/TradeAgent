---
name: technical-basic
description: Historical price-based analytics.
---

# technical-basic

Combines historical trend, momentum, volatility bands and volume confirmation from daily OHLCV. Requires complete bars; missing or insufficient history produces a partial result.

The fixed startup analyst requires the `prices` capability. Run through the
shared native, HTTP, MCP, or `trade-research run-skill technical-basic SYMBOL` interface.
Outputs use the common observation, provenance and limitation schema.
Implementation: `src/trade_research/skills/price_series.py`.
