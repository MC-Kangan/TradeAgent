---
name: risk-analysis
description: Historical price-based analytics.
---

# risk-analysis

Computes historical volatility, tail-loss, drawdown and return-shape statistics from the configured price history. This is historical risk evidence, not a loss guarantee or position-sizing instruction.

The fixed startup analyst requires the `prices` capability. Run through the
shared native, HTTP, MCP, or `trade-research run-skill risk-analysis SYMBOL` interface.
Outputs use the common observation, provenance and limitation schema.
Implementation: `src/trade_research/skills/price_series.py`.
