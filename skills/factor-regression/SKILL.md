---
name: factor-regression
description: Explain historical stock returns using configurable ETF, research and normalized change factors.
---

# Factor regression

Use the fixed startup analyst through native Python, HTTP POST /analyze, MCP
run_skill or trade-research analyze --request-file. It is immediate-only;
raw histories must not be queued or persisted in reports.

Read [configuration, contracts and interpretation](../../docs/factor-mvp2-implementation.md)
for the current factor-regression-v4 contract. Presets expand to ordinary factor
lists. Explicit factors replace the preset; no fixed market/growth/value/momentum
or single-industry parameter fields remain.

Use daily or monthly frequency explicitly. Never interpolate monthly factors into
daily data. Native monthly asset returns require explicit exchange-month endpoints.
Research values require declared semantics and units. Cash is subtracted exactly
once from funded returns in excess-return mode. Missing internal periods withhold
HAC intervals; inspect coverage and diagnostics before interpreting coefficients.

These are explanatory regressions, not forecasts, causal estimates or investment
recommendations. Large factor counts need substantially more data than the validation
minimum. Correlated regressors can make individual coefficients unstable. No PCA or
orthogonalization is performed automatically. Explicit residualization and named
comparisons are documented in [model comparison](../../docs/factor-model-comparison.md).

Yahoo uses adjusted closes for equities/ETFs and rejects unverified index histories.
A price index is not a total-return index. Use verified Bloomberg mappings for entitled
indexes. GBp/GBX normalization changes currency labels for dimensionless returns,
not economic currency exposure. Ambiguous EU/EURONEXT listings require a precise venue
or an explicit provider mapping.

## Run

```sh
TRADE_RESEARCH_PRICE_PROVIDER=yahoo .venv/bin/trade-research run-skill factor-regression AAPL
.venv/bin/python examples/factor_regression.py --region europe
```

The example is synthetic and requires no network. For licensed data, supply normalized
factor_series, research_factors and fx_series on an authorized local request, or inject
typed providers. The CLI input limit remains 4 MiB. Keep source vintages in authorized
storage if exact reproduction is required; reports retain metadata and hashes only.

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


## Deferred work

Cross-sectional signal evaluation, PCA/ridge, dedicated macro connectors and crypto
are subsequent increments. Use the implementation notes for source/reuse decisions.
