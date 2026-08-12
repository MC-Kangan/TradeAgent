# Asset Allocation Skill

**Name:** `asset-allocation`
**Scope:** portfolio
**Required capability:** `PRICES`
**Implementation:** `src/trade_research/skills/portfolio.py`
**Methodology reference:** [HKUDS/Vibe-Trading asset-allocation](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/src/skills/asset-allocation/SKILL.md)

## Purpose

Create a deterministic, long-only, fully invested allocation scenario for 2–9
assets from aligned historical prices. It never reads broker accounts, places
orders, or labels the weights as personalized targets.

The first release adapts the price-only frameworks documented by Vibe-Trading.
Black–Litterman, expected-return mean variance, and turnover-aware optimization
are intentionally excluded because their required views, forecasts, current
weights, and trading-cost inputs are not part of this contract.

## Parameters

| Parameter | Default | Constraint |
|---|---:|---|
| `lookback` | 120 | 20–252 aligned daily returns |
| `method` | `risk_parity` | One of the four methods below |

## Methods

| Method | Calculation | Main limitation |
|---|---|---|
| `equal_weight` | `wᵢ = 1/N` | Ignores volatility and correlation |
| `inverse_volatility` | Normalize `1/σᵢ` | Ignores cross-asset correlation |
| `risk_parity` | Solve equal variance-risk budgets by coordinate descent | Depends on the covariance estimate |
| `max_diversification` | Long-only projection of `Σ⁻¹σ`, with a small numerical ridge | Can concentrate when correlations are unstable |

All methods enforce `wᵢ ≥ 0` and `Σwᵢ = 1`. No leverage or short positions are
produced.

## Output

- Scenario weight and variance-risk contribution for each asset.
- Annualized asset and portfolio volatility.
- Correlation matrix used by the calculation.
- Diversification ratio `(wᵀσ) / σₚ`.
- Effective asset count `1 / Σwᵢ²`.

Mixed equity/crypto portfolios use aligned common dates and 252-day
annualization; crypto-only portfolios use 365 days.

## Interpretation and limitations

Weights are mathematical scenarios, not buy/sell instructions. Historical
prices do not encode expected returns, taxes, liquidity, FX exposure, cash
needs, transaction costs, investment objectives, or future correlation shifts.
Risk parity balances modeled variance contribution; it does not make every
asset equally safe. Maximum diversification optimizes a historical ratio and
can be fragile out of sample.

Vibe-Trading is MIT-licensed. This implementation uses an original typed,
bounded calculation that follows the documented allocation concepts rather
than copying its runtime or optimizer source.

## Failure modes

- A request without 2–9 typed portfolio constituents is rejected at the request boundary.
- Missing or insufficient history returns a partial report with an explicit limitation.
- Singular covariance faces are skipped by the maximum-diversification solver.
- If no valid diversified face exists, the method degrades to inverse-volatility weights.

Portfolio constituents are supplied through the request-level
`portfolio_instruments` field. Reports use `PORTFOLIO:BASKET` and include
auditable content-hash references for every aligned price series.
