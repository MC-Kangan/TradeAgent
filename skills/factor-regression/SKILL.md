---
name: factor-regression
description: Explain one stock's historical total returns with a fixed three-factor OLS model.
---

# Factor regression — MVP 1

The fixed startup analyst `factor-regression` runs through native Python,
`POST /analyze`, MCP `run_skill`, or `trade-research analyze --request-file request.json`.
It is immediate-only. The durable queue rejects it rather than losing parameters
or persisting licensed input histories. No frontend or new service is required.

## Model and interpretation

`stock_return = intercept + market_beta * market_return + style_beta *
(growth_return - value_return) + momentum_beta * (momentum_return - market_return) + residual`

All returns are simple decimal total returns (0.01 means 1%). This is historical
explanation, not a forecast, causal model, or trading recommendation. The market
leg is a raw return, so the intercept is **not risk-adjusted alpha**. Style and
momentum legs are return differences; their coefficients are dimensionless.
An exposure of 1.2 means a 1 percentage-point factor move is associated with
1.2 percentage points of stock return, holding the other regressors fixed.

OLS includes an intercept. statsmodels computes Newey–West/HAC covariance with
Bartlett weights, small-sample correction and Student-t confidence intervals.
Default lag count is 5, minimum sample is 252, and the default window is the
three years ending yesterday (UTC). Minimum sample may be lowered to 60, with a
short-history diagnostic below 252. Dates must precede today and span at most ten
years. Defaults are resolved against the engine's UTC clock before execution;
invalid resolved windows are request-validation errors. Confidence intervals are pointwise, not multiple-testing adjusted.

Reports contain coefficients, 95% intervals, standardized effects, R-squared,
adjusted R-squared, residual volatility per observed interval (not annualized),
regressor correlation matrix, variance inflation factors, and the standardized
design condition number. VIF above 10 or condition number above 30 produces a
warning; exact rank deficiency produces a partial result without coefficients.
The JSON correlation/VIF order is market, growth-minus-value, momentum-minus-market.

## Inputs and alignment

`FactorReturnSeries` is the common provider contract: instrument, ISO currency,
return basis, provider, vendor field, retrieval time and bounded dated intervals.
Return points specify **both** start and end dates. Up to five series, each at
most 4,096 intervals, may be supplied in `AnalysisRequest.factor_series`.
Input histories are not included in saved or exported factor reports. Reports
retain explicit input metadata and content hashes; keep underlying licensed
snapshots only in your firm's authorized storage if reproducibility is needed.

Alignment intersects complete `(start_date, end_date)` intervals. It never
forward-fills, invents FX conversion, mixes return bases, or bridges missing
prices. Intervals over four calendar days are excluded. Differing holidays may
therefore reduce the sample. `discontinuity_count` counts gaps between retained
return intervals, including gaps shared by every input; `discontinuous_history`
flags them. This is separate from `dropped_interval_count`, which counts supplied
intervals lost during alignment/filtering. Discontinuities do not estimate the
number of missing sessions or detect missing data outside the retained window.
HAC lags count retained observations, not calendar days. Dates do not synchronize intraday market closes; all reports retain that
limitation. A mixed-currency or mixed-basis study returns partial diagnostics.

Yahoo uses `indicators.adjclose` and the exchange timezone, and rejects missing
adjustments. Instrument metadata must identify an equity or ETF. Indexes and
unknown instrument types are rejected: an adjusted-close field does not prove
that an index includes dividends. Use explicitly verified Bloomberg total-return
mappings or normalized authorized-local inputs for index benchmarks. Ordinary OHLCV remains separate. London GBp/GBX is normalized to GBP
for dimensionless returns; it does not convert currencies. Ambiguous `EU` and
`EURONEXT` Yahoo listings are rejected. Use precise supported venues such as
`LSE`, `XETRA`, `SIX`, `BME`, or `BORSA_ITALIANA`, or an explicit Bloomberg mapping.

## Benchmarks

The `us_etf` preset uses IWB (market), IWF (growth), IWD (value), and MTUM
(momentum). These are investable fund proxies, with fees, tracking differences,
and non-identical methodologies; they are not academic Fama–French factors or
pure orthogonal style portfolios. The model does not remove their correlations.
It reports diagnostics so you can judge whether coefficients are stable enough
to interpret.

For Europe, use `preset: custom` and supply four explicit `InstrumentId` objects
under `market_benchmark`, `growth_benchmark`, `value_benchmark`, and
`momentum_benchmark`. A sensible institutional starting point is MSCI Europe,
MSCI Europe Growth, MSCI Europe Value, and MSCI Europe Momentum, all in the same
currency and gross/net convention as the stock. Verify your entitled Bloomberg
identifiers and field definitions with your firm's data team. No Bloomberg
identifiers or automatic currency conversions are guessed by this implementation.
The synthetic European example demonstrates the contract, not real index data.

## Run it

Yahoo U.S. example:

```sh
TRADE_RESEARCH_PRICE_PROVIDER=yahoo .venv/bin/trade-research run-skill factor-regression AAPL
```

For explicit dates, put this in an authorized local `request.json`:

```json
{
  "instrument": {"symbol": "AAPL", "market": "US"},
  "analysts": ["factor-regression"],
  "skill_parameters": {
    "factor-regression": {
      "preset": "us_etf",
      "start_date": "2023-01-01",
      "end_date": "2025-12-31",
      "minimum_observations": 252,
      "hac_lags": 5
    }
  }
}
```

```sh
TRADE_RESEARCH_PRICE_PROVIDER=yahoo .venv/bin/trade-research analyze --request-file request.json
.venv/bin/python examples/factor_regression.py --region europe
```

The example uses synthetic returns and requires no network. For local licensed
data, normalize histories into `factor_series` on the same request. The CLI
accepts at most 4 MiB and uses the same validated request and report schema.

## Bloomberg and B-PIPE integration

Set `price_provider: bloomberg` and `bloomberg_return_mappings` in the existing
JSON settings file referenced by `TRADE_RESEARCH_CONFIG`. Each mapping contains
`instrument`, `security`, `field`, `currency`, and `return_basis`.
`TOTAL_RETURN_INDEX_GROSS_DVDS` requires `gross_total_return`. `PX_LAST` is accepted
only for `INDEX` instruments explicitly verified to be total-return indexes;
using a price index and labelling it total return would invalidate the study.
No mapping defaults are supplied. Host/port configuration uses the existing
Bloomberg settings and opens an owned session per request.

Firm B-PIPE integrations can inject a dedicated started, authorized `blpapi`
session and its identity into `BloombergReturnProvider(mappings, session=...,
identity=...)`, registered as `factor_returns` in `ProviderRegistry`. The adapter
serializes requests and correlates responses. The caller owns that session and
must not consume its events elsewhere. Authentication, entitlement and security
mapping remain firm-specific; this does not provide automatic B-PIPE login.
It uses the historical reference-data service, not a streaming subscription.
Live Bloomberg connectivity must be verified in the authorized firm environment.

## Reuse decision and deferred work

We reviewed [Vibe-Trading's factor core](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/src/factors/factor_analysis_core.py)
and borrowed the explicit paired-alignment idea, without importing its platform
or copying its implementation. Numerical estimation uses
[statsmodels OLS](https://www.statsmodels.org/stable/generated/statsmodels.regression.linear_model.OLS.html)
and its [robust covariance implementation](https://www.statsmodels.org/stable/generated/statsmodels.regression.linear_model.RegressionResults.get_robustcov_results.html).
The Bloomberg adapter follows the public
[request](https://bloomberg.github.io/blpapi-docs/python/3.26.5.1/_autosummary/blpapi.Request.html)
and [session](https://bloomberg.github.io/blpapi-docs/python/3.26.9/_autosummary/blpapi.Session.html)
contracts. Qlib and Alphalens were reviewed in the framework plan but not added.

Risk-free/excess-return alpha, FX conversion, rolling models, commodity factors,
orthogonalization/PCA, cross-sectional evaluation and crypto are later increments.
