# markov-method

Markov regime detection skill for the TradeAgent research framework.

Labels each trading day Bull/Bear/Sideways via rolling returns, builds a 3×3
Markov transition matrix, computes the stationary distribution, and emits a
signed signal (bull_prob − bear_prob) that can serve as a trade direction
filter, risk-management overlay, or standalone signal.

## Original Algorithm

https://github.com/jackson-video-resources/markov-hedge-fund-method

Framework by Roan (@RohOnChain); refactored into plugin form by Lewis Jackson.

## Quick Start

```sh
# Requires a PRICES provider (e.g., Yahoo)
export TRADE_RESEARCH_PRICE_PROVIDER=yahoo

# Run on any symbol
.venv/bin/trade-research run-skill markov-method AAPL

# With custom parameters (edit src/trade_research/skills/markov_method.py fields)
# window: int = 20       — lookback for rolling returns
# threshold: float = 0.05 — ±5% regime boundary
# min_train: int = 252   — minimum training bars
```

## Output

Returns an `AnalystResult` with:
- **Current regime**: Bull, Bear, or Sideways
- **Signal**: −1 to +1 (positive = bullish bias)
- **Stationary distribution**: long-run regime probabilities
- **Persistence diagonal**: how sticky each regime is
- **Optional walk-forward backtest**: Sharpe ratio + max drawdown

## Configuration

See [SKILL.md](SKILL.md) for the full algorithm specification, metrics table,
and failure modes.
