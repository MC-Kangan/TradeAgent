---
name: volatility-regime
description: Historical price-based analytics.
---

# volatility-regime

Classifies recent realized volatility as compressed, normal or elevated relative to its historical distribution. It describes the past, without forecasting a future regime.

The fixed startup analyst requires the `prices` capability. Run through the
shared native, HTTP, MCP, or `trade-research run-skill volatility-regime SYMBOL` interface.
Outputs use the common observation, provenance and limitation schema.
Implementation: `src/trade_research/skills/price_series.py`.
