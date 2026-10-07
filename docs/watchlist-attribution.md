# Watchlist attribution MVP

Factor Playground now exposes a separate Watchlist attribution tab. Select up to
20 Bloomberg equity identifiers, a shared set of factor packs, selected factors,
provider, research region, training start, minimum observations and HAC lags.
Manual refresh and optional 5-minute, 15-minute or hourly refresh run the same
backend workflow. Automatic refresh pauses on other tabs. Each refresh replaces
all rows; unavailable stocks never retain silently stale contributions.

## Estimation and reconciliation

`ResearchEngine.factor_monitor_snapshot(request)` uses `prepare_study` for all
provider normalization, currency conversion, return/excess-return conventions,
factor transformations and calendar alignment. It accepts daily original-factor
models only. It holds out the last aligned observation, then calls the existing
`fit_study` OLS/HAC estimator using strictly preceding observations. The minimum
sample applies to the training data, excluding the held-out observation.

For the displayed interval:

- factor contribution = fitted beta × the same normalized factor’s observed move;
- explained return = intercept + sum of factor contributions;
- unexplained return = actual normalized stock return − explained return.

The UI multiplies decimal contributions by 10,000 to display basis points. Yield
and commodity changes retain their declared input units. The table shows currency
and return basis: an excess USD return is not a local price change. It also shows
data date, training end, adjusted R², sample size and maximum VIF. Row selection
opens a fixed-height contribution chart, factor moves and model warnings.

The UI excludes the current UTC date, preventing partial daily observations from
being labelled completed closes. The newest aligned date can lag because of
missing observations, research factor publication delays or market holidays.
`Lagged data` compares that date with the last expected stock exchange session.
Rows must not be assumed to share the same date. Missing data is not replaced
with zero and French factors are not replaced by ETF proxies.

## Interpretation and scope

This estimates contemporaneous exposures; it is not a price forecast, causal
explanation or point-in-time backtest. Historical sources can be revised. Asynchronous
market closes, omitted factors and parameter uncertainty affect the unexplained
return. Training adjusted R² is not a measure of held-out prediction accuracy.

OLS does not require orthogonal factors. Exact redundancy is rejected and strong
collinearity is diagnosed. Sequential residualization changes the attribution
basis and depends on ordering; it does not create information or remove omitted
variable bias. Keep the monitor in original economic factor units and use the
single-stock diagnostics to investigate overlap. Never multiply a residualized
beta by an untransformed factor move.

Each refresh currently refits all rows; there is no persistent beta cache, streaming
subscription, sector classification service or per-stock factor override. The
20-stock bound limits synchronous work. Models and snapshots remain in memory.
No additional dependencies are required. Bloomberg connectivity and entitlements
must be checked on the company machine; local verification uses synthetic fixtures.

## Ideas borrowed

- The supplied prototype: a stock-by-factor table with observed, model and unexplained
  return, plus refresh controls. Its statistical implementation was not copied.
- [Pyfolio attribution documentation](https://pyfolio.ml4trading.io/api-reference.html):
  separate exposures, factor moves and contribution reporting. We reuse the existing
  backend rather than add Pyfolio as a dependency.
- [Dash live updates](https://dash.plotly.com/live-updates): use a bounded `dcc.Interval`
  callback for refresh. No separate worker, message broker or streaming framework.

## Related consistency fixes

European Bloomberg exchanges now resolve to the installed exchange calendars.
Explicit Bloomberg return mappings take precedence over exchange-derived defaults.
YAML level factors carry `research_source: pack`; French presets carry
`research_source: kenneth_french`. A pack factor named momentum no longer implies
French cash treatment. Combined sources still require unique research keys; the
shipped MSCI template uses `msci_momentum` to coexist with French `momentum`.
