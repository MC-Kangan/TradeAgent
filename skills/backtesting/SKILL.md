# Backtesting analyst contract

## Inputs

- One instrument and 2–520 timezone-aware daily bars.
- Positive, finite Open, High, Low, and Close values; Volume is optional.
- One validated built-in strategy configuration or bounded external signal list.
- Explicit cash, commission, spread, position size, and optional stop/target assumptions.
- Optional performance start date plus a minimum holding period of 1–520 daily bars.

## Method

Generate causal entry/exit booleans from data available through each bar. Run
the fixed long/flat execution strategy using pinned `backtesting.py` 0.6.6.
Orders generated at bar t execute at bar t+1 open. Early exit signals remain
pending until the minimum hold is satisfied. An open trade is marked to the
last close rather than forcibly sold. Crypto uses the
engine's fractional-unit adapter; equities retain whole-share execution.

## Output

- Final equity, strategy and buy/hold returns, maximum drawdown, trade count,
  win rate, and Sharpe ratio when the engine can calculate them.
- At most 520 equity/drawdown points and 200 closed trades.
- Bounded daily price and strategy-indicator series plus an optional open-position snapshot.
- SHA-256 references for input prices and the derived signal vector.
- Complete bounded strategy, capital, cost, sizing, and protective-level
  assumptions with a canonical configuration reference.
- `not_assessed` directional signal: backtest results do not become a live view.

## Safety and limitations

This is analytics-only. Never accept executable strategy code, broker state,
orders, arbitrary SQL, or optimizer requests. External signal payloads are not
copied into reports or the durable queue. Misaligned signals and incomplete
OHLC input produce a typed partial result.
Protective levels are derived from the actual fill and become active on the
following bar. Entry signals that cannot obtain a fill produce a partial result.
