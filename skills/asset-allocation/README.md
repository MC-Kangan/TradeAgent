# Asset Allocation

`asset-allocation` creates deterministic long-only allocation scenarios for
2–9 instruments from aligned daily prices.

Methodology reference: [HKUDS/Vibe-Trading asset allocation](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/src/skills/asset-allocation/SKILL.md).

Quick start:

```bash
trade-research run-skill asset-allocation BASKET \
  --portfolio-instrument US:SPY \
  --portfolio-instrument US:TLT \
  --method risk_parity \
  --lookback 120
```

The output includes scenario weights, risk contributions, portfolio volatility,
diversification ratio, effective asset count, and input-series provenance.

See [SKILL.md](SKILL.md) for formulas, failure modes, and interpretation.
