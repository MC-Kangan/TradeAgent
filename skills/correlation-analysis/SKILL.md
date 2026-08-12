# Correlation Analysis Skill

**Name:** `correlation-analysis`
**Scope:** portfolio
**Required capability:** `PRICES`
**Implementation:** `src/trade_research/skills/portfolio.py`

## Purpose

Align the daily closes of 2–9 instruments, calculate simple returns over a
bounded lookback, and return a symmetric Pearson correlation matrix. The skill
is descriptive and emits no directional signal.

## Parameters

| Parameter | Default | Constraint |
|---|---:|---|
| `lookback` | 120 | 20–252 aligned daily returns |

## Method

1. Validate and sort each series through the shared price contract.
2. Intersect exact UTC observation timestamps across every selected asset.
3. Keep the latest `lookback + 1` common closes and compute simple returns.
4. Calculate the sample covariance matrix and convert it to correlations.
5. Return annualized asset volatility and the bounded matrix presentation.

Mixed equity/crypto portfolios use the common observation calendar and 252-day
annualization. Crypto-only portfolios use 365 days. This avoids filling missing
stock weekend prices with artificial zero returns.

## Interpretation

| Correlation | Interpretation |
|---:|---|
| Near `+1` | Assets historically moved together |
| Near `0` | Little linear co-movement in the sample |
| Near `-1` | Assets historically moved in opposite directions |

Correlation is unstable during market stress and does not establish causality.
The output is historical evidence, not a forecast.

## Failure modes

- Fewer than two or more than nine instruments: partial report.
- Any instrument with fewer than 21 valid closes: partial report naming the gap.
- Fewer than 21 aligned closes after calendar intersection: partial report.
- A zero-volatility asset receives zero off-diagonal correlations rather than
  an undefined or non-finite value.

The portfolio constituents are supplied through the typed request-level
`portfolio_instruments` field, not encoded inside skill parameters. Reports use
the canonical `PORTFOLIO:BASKET` identity and retain one content-hash reference
per aligned input series.
