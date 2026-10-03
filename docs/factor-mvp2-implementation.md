# Factor research MVP 2A

Single-stock historical explanation using a configurable list of factors. Cross-sectional
signal evaluation (MVP 2B), PCA/ridge, commodity connectors and VibeResearch integration
remain separate increments. Normalized macro changes can already be supplied by an
internal research-factor provider; no macro data is fetched or transformed implicitly.

## Configuration

Use the existing `factor-regression` analyst. A preset is only a default factor list:
`us_etf` selects market, growth-minus-value and momentum-minus-market;
`french` selects FF5 plus momentum. An explicit `factors` list replaces the preset
selection completely. `custom` requires an explicit list.

For example, a monthly model with market excess return, value and pharma:

```json
{
  "preset": "french",
  "region": "US",
  "frequency": "monthly",
  "start_date": "2017-01-01",
  "end_date": "2025-12-31",
  "factors": [
    {"id": "market", "label": "Market excess", "kind": "research", "research_key": "market_excess"},
    {"id": "value", "label": "Value", "kind": "research", "research_key": "hml"},
    {"id": "pharma", "label": "Pharma ETF", "kind": "asset_return",
     "instrument": {"market": "US", "symbol": "IHE"}}
  ],
  "baseline_factor_ids": ["market", "value"]
}
```

Supply this under `skill_parameters.factor-regression` in an ordinary `AnalysisRequest`.
Any number of factors within a bounded budget is supported: default `max_factors=32`,
configurable up to the shared 64-factor safety bound. Minimum observations and rolling
window must exceed factor count plus intercept plus HAC lags. This is a validation
floor, not a claim that a large model has enough statistical power.

Factor kinds:
- `research`: select a named `research_key` from a normalized panel.
- `asset_return`: one total-return `instrument`.
- `spread`: long `instrument` minus `short_instrument`.

Each selection has a unique ID and a display label. Asset legs accept `calendar` and
`short_calendar`; `stock_calendar` selects the target calendar. Ordinary listings
use provider calendar metadata or the instrument's venue. Explicit calendar names
come from exchange_calendars; `weekdays` is available for research calendar conventions.
Shared instruments are fetched once. Conflicting explicit calendars fail closed.

`return_mode` is `excess_return` by default for French and `raw_total_return` otherwise.
It can be set explicitly. Cash is subtracted once from target and funded asset factors;
already-excess research returns, spreads and normalized changes are not reduced again.
Raw-mode intercepts are not risk-adjusted alpha.

Daily defaults: three years, 252 minimum observations, HAC lag 5, rolling window 252.
Monthly defaults: ten years, 60 minimum observations, lag 3, window 60. Explicit overrides
are respected. Only daily/monthly are implemented; the contract rejects upsampling.
All study windows are historical and at most ten years. Old fixed benchmark and
single-industry parameter fields have been removed.

## Providers and standardized observations

`FactorReturnSeries` carries source frequency, calendar, ISO currency, return basis,
vendor field, retrieval timestamp and explicit opening/closing dates. Daily inputs
compound into complete monthly returns; native monthly total returns are accepted
directly when their endpoints match the exchange month. Missing sessions or partial
months are never silently filled.

`ResearchFactorPanel` is provider-independent: region, native frequency, currency,
calendar, definitions, dated value dictionaries, optional cash returns and safe hash.
Definitions state IDs, labels, semantics (asset/excess/spread/change), and units
(decimal return, basis points or percentage points). An internal or Bloomberg adapter
can supply different columns and currency without changing the estimator. Cash is
required only for excess-return studies. Changes must be normalized by the provider;
raw economic levels are not automatically differenced or interpreted.

The French adapter downloads FF5 and momentum from the official US/Europe daily or
monthly files, normalizes percentages, filters missing sentinels and rejects malformed
dates/values. It is responsible for its six-column catalog and USD convention.
Those restrictions do not live in the generic model. French data may lag and be revised;
this is not point-in-time predictive research.

`FxLevelSeries` states base and quote currency. Conversion is
`(1 + local_return) * fx_end / fx_start - 1`. Required dates are derived from actual
return endpoints, including the close preceding the first month. Yahoo currently
supports EUR/GBP/CHF to USD; injected/inline providers can supply other explicit pairs.
Bloomberg never borrows Yahoo FX implicitly. Different currency assets are converted
to the panel currency, or the stock currency if no research panel is used.

Provider adapters own vendor parsing and entitlement handling. Use verified Bloomberg
total-return mappings, an injected firm adapter, or normalized inline inputs. B-PIPE
session authentication remains firm-specific. No live Bloomberg verification was possible.

## Inference and coverage

The estimator uses statsmodels OLS with an intercept and standardized numerical design,
then transforms coefficients and covariance back to original units. Standardized effects,
correlations, VIF and condition number help compare factors and diagnose collinearity.
No PCA, residualization or automatic factor selection is applied.

For consecutive retained calendar periods, pointwise 95% intervals use Bartlett HAC
with the selected lag count, finite-sample correction and Student-t critical values.
If internal periods are missing, OLS exposures remain available but HAC standard errors,
confidence intervals and residual lag-one correlation are withheld. Treating compressed
rows as adjacent would violate ordinary HAC's equally spaced observation assumption.
No uncertainty claim is made for gapped samples.

Reports (`factor-regression-v3`) expose each asset's expected periods, available complete
periods, missing/invalid periods, FX losses and alignment losses. The aggregate dropped
count is expected target periods minus fitted observations; it includes missing leading
months. Incomplete boundary months outside the selected complete-month window are
not expected periods. Discontinuity counts refer to internal gaps.

Rolling windows use only their own observations and their own scaling. Month-end refits
have no future information; singular windows are skipped. Rolling estimates have no
confidence bands. Optional `baseline_factor_ids` compare a strict subset on exactly the
same sample. Additional R² measures explanation, not forecasting improvement. Funded
sector benchmarks may contain the target; constituent weights are not checked.
Residual diagnostics flag influential data without removing market shocks.

Daily cross-market closes may be asynchronous. French Europe uses weekdays; a local
return spanning several research dates is excluded, not paired with one factor day.
Monthly alignment reduces this mismatch but does not synchronize intraday FX closes.

## Maintenance and reuse

Modules separate catalog (`domain/factors.py`), parameter validation
(`factor_parameters.py`), calendar/FX transformations (`factor_data.py`),
provider assembly/alignment (`factor_study.py`), and estimation (`factor_regression.py`).
CLI, HTTP, MCP and native calls use the same path. Failure metadata contains a stage,
safe code and reference; reports do not include raw histories or licensed payloads.
Factor studies and inline research/FX inputs are immediate-only, never durable queued.

We reviewed [Alphalens Reloaded](https://github.com/stefan-jansen/alphalens-reloaded),
[statsmodels](https://www.statsmodels.org/stable/examples/notebooks/generated/rolling_ls.html),
[exchange_calendars](https://github.com/gerrymanoim/exchange_calendars) and the
[Kenneth French library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html).
We borrow workflow ideas without importing another research platform. Estimation stays
in statsmodels; the only added dependency family is exchange calendars.

The standalone Dash app remains in the sibling FactorPlayground directory. It exposes
preset subsets, multiple extra benchmarks/spreads, advanced factor lists, provider
selection, frequency and diagnostics. It calls the same backend and stores no histories.
