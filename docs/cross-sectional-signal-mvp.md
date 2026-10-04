# Cross-sectional signal MVP V2

The `cross-sectional-signal` analyst evaluates whether a stock characteristic ranks
future returns across an explicitly supplied universe. It is separate from
`factor-regression`, which explains one stock's contemporaneous historical returns.

This first slice implements one transparent signal: **12-1 momentum**. At each calendar
month-end it compounds returns from months t-12 through t-2, omitting the most recent
completed month to reduce short-term reversal contamination. It ranks eligible stocks,
enters at each listing's next session close, and exits at the next rebalance session
close. No same-close execution is assumed.

The report includes monthly Spearman rank information coefficients, equal-weighted
quantile returns, top-minus-bottom diagnostic returns, top-quantile turnover, per-stock
coverage and explicit insufficient-data diagnostics. V2 adds dated universe membership,
sector/country grouping, a date-block-bootstrap interval for mean IC, IC consistency
statistics, chronological development/holdout summaries and a long-only top-quantile
view against the eligible universe. Configurable transaction costs are illustrative;
funding, borrow and market impact remain excluded.

## Request shape

Use a portfolio-scoped request with 3–64 unique instruments and select only the
`cross-sectional-signal` analyst. Normalized daily total-return histories may be fetched
from the configured return provider or supplied through `factor_series`:

```json
{
  "instrument": {"symbol": "BASKET", "market": "PORTFOLIO"},
  "scope": "portfolio",
  "portfolio_instruments": [
    {"symbol": "AAPL", "market": "US"},
    {"symbol": "MSFT", "market": "US"},
    {"symbol": "NVDA", "market": "US"}
  ],
  "analysts": ["cross-sectional-signal"],
  "skill_parameters": {
    "cross-sectional-signal": {
      "start_date": "2020-01-01",
      "end_date": "2026-09-30",
      "quantiles": 3,
      "minimum_assets": 3,
      "universe_mode": "current_watchlist",
      "grouping": "overall",
      "transaction_cost_bps": 10,
      "holdout_start_date": "2024-01-01"
    }
  }
}
```

Yahoo uses its adjusted-close return adapter for this study; its ordinary OHLCV feed is
not substituted. Bloomberg can use administrator-verified total-return mappings. All
histories in one run must use the same currency and return basis. This makes mixed-currency
European studies fail explicitly until the inputs have been normalized to a chosen study
currency.

The current-universe diagnostic is deliberate. A list of today's surviving stocks is
useful for watchlist research but does not establish a survivorship-free historical
universe. Set `universe_mode` to `point_in_time` and provide one member record per
portfolio instrument with `member_from`, optional `member_to`, sector and country.
Point-in-time mode controls formation-date eligibility; it does not invent delisting
returns or guarantee that a vendor retained dead securities, so terminal-return coverage
remains explicit.

`grouping` may be `overall`, `sector`, `country` or `sector_country`. Grouped runs assign
quantiles within each sufficiently large group, calculate group ICs separately, and then
equal-weight valid groups. This avoids allowing a large sector to dominate the reported
signal, but small groups are excluded rather than forced into unreliable quantiles.

The IC information ratio is monthly mean IC divided by monthly IC standard deviation.
The 95% interval uses deterministic circular date blocks, preserving short runs of
adjacent formation dates instead of treating stock-month rows as independent samples.
Development and holdout dates remain chronological; there is no random row split.
The development formation whose forward holding interval crosses the holdout boundary
is purged and counted separately.

The top-quantile portfolio is equal-weighted for an overall run and equal-weights valid
groups for grouped runs. Turnover is one-half the absolute change in portfolio weights.
The initial deployment and any restart after an unevaluable formation month report
turnover one. Reported net returns subtract `transaction_cost_bps × turnover`; this is a
sensitivity assumption, not an estimate of actual execution costs.
Annualized return, annualized volatility and drawdown are withheld when valid portfolio
formation dates are discontinuous; average period results remain available.

Point-in-time fundamentals, delisting-return connectors, liquidity screens and multiple
signal definitions remain later increments.

## Reuse decision

The date/asset panel, forward-return alignment, rank IC, quantile-return and turnover
concepts follow the research workflow popularized by
[Alphalens Reloaded](https://github.com/stefan-jansen/alphalens-reloaded). The implementation
uses the project's existing NumPy/SciPy and normalized provider contracts. Alphalens,
Qlib and vectorbt are not runtime dependencies.
