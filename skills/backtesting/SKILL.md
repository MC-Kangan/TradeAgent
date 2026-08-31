# Backtesting analyst contract

## Inputs

- One instrument and 2–4,096 timezone-aware daily bars.
- Positive, finite Open, High, Low, and Close values; Volume is optional.
- One validated built-in strategy configuration or bounded external signal list.
- Explicit position budget, commission, spread, tranche fraction, deployment
  cap, addition cooldown, and optional stop/target assumptions.
- Optional performance start date plus a minimum holding period of 1–520 daily bars.

## Method

Generate causal add/reduce/exit actions from data available through each bar. Run
the fixed long-only tranche strategy using pinned `backtesting.py` 0.6.6.
Orders generated at bar t execute at bar t+1 open. Each addition targets the
configured percentage of the single-position budget and creates a fractional
lot when sufficient cash and deployment headroom are available and its cooldown
has elapsed. Deployment uses the entry notional of open lots, not their changing
market value. A reduction closes the oldest eligible lot; an exit closes every
eligible lot. Sell actions remain pending until the minimum hold is satisfied.
Open lots are marked to the last close rather than forcibly sold. Every market
uses the engine's fractional-unit adapter. Built-in strategies add only on
indicator or regime transitions and exit fully when their thesis reverses.

## Output

- Separate fixed-horizon entry-signal quality, signal-to-execution audit, and
  net position-performance sections derived from one canonical event stream.
- Signal count, non-overlapping outcome count, expectancy, win rate,
  winner/loser payoff, MFE, MAE, and a descriptive chronological holdout view
  beginning at the final 20% of the performance window.
- Addition, reduction, and exit audit counts; delayed and ignored actions;
  rejected additions by reason; separate unfilled addition/reduction/exit
  boundary counts; average entry fill; explicit commissions; and average/maximum
  cost-basis deployment and exposure.
- Calculation coverage, warm-up size, discarded-row count, quote currency,
  price-adjustment convention, and daily-boundary convention.
- Final equity, reconcilable net realized/open unrealized/total P/L, strategy
  return, return on average deployed capital, gross two-sided turnover, maximum
  drawdown, trade count, win rate, payoff, expectancy, profit factor, and Sharpe
  ratio when calculable.
- Full-investment buy-and-hold plus a capital-use comparison equal to buy-and-hold
  return multiplied by average cost-basis exposure.
- Full-history calculations with extrema-aware equity sampling, at most 520
  chart points, 200 displayed closed trades, and 100 displayed open-lot snapshots.
- Full open-position aggregates even when individual snapshots are reduced.
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
The final-20% holdout is descriptive and must not be described as untouched
out-of-sample evidence when the strategy was developed on the same history.
