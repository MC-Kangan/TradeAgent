# Model comparison, stability and explicit attribution

This increment is sector-independent. Market, sector, style and business-driver
factors are ordinary configurable inputs. European energy is an offline illustration,
not a special estimator or a default list of instruments.

## Compare models

Select the union of factors in `factors`, then name up to eight proper subsets:

```json
{
  "comparisons": [
    {"name": "Market", "factor_ids": ["market"]},
    {"name": "Market and sector", "factor_ids": ["market", "sector"]},
    {"name": "Market and business drivers", "factor_ids": ["market", "oil", "gas"]}
  ],
  "residualizations": [
    {"factor_id": "sector", "against": ["market"]}
  ]
}
```

Every comparison uses the full model's already-aligned dates, target returns, currency,
cash convention and source vintages. A smaller model does not recover extra dates
lost to another factor's missing history. This sacrifices sample size to make the
comparison fair. The automatic **Full model** row and all named comparison rows use
original, untransformed factors.

Outputs include sample count, R², adjusted R², residual standard deviation (with
residual degrees of freedom), standardized-design condition number, VIF, a collinearity flag and coefficient
point estimates. Markdown exports include these original-model diagnostics and
coefficients separately from the selected (possibly residualized) basis. An original-model
collinearity warning remains visible after residualization. Comparing shared coefficients shows sensitivity to model specification.
There is no automated winner, p-value screening or best-subset search. Adding regressors
cannot reduce ordinary in-sample R²; an increase alone is not investment evidence.

The named `comparisons` contract replaces `baseline_factor_ids` and the single
baseline/incremental-R² output. Report schema is `factor-regression-v4`.

## Optional residualization

A rule replaces its target factor with the residual from an intercept-inclusive
regression against the specified controls. The controls remain in the full model.
All rules use original control columns simultaneously: a target cannot also be
another rule's control. This prevents hidden ordering, chains and cycles.

This is a linear reparameterization, so it preserves fitted returns and R² for a
full-rank model. It changes attribution: control coefficients absorb the component
removed from the target. The coefficient table and rolling plot use this selected
basis; comparison rows retain the original basis so the difference remains visible.

The residual is orthogonal to its controls within the fitted sample. Residualized
targets can still correlate with each other and with uncontrolled factors.
Orthogonality does not imply statistical independence or causality.

Each rule reports the fraction of original variance remaining. A tiny fraction
means little independent movement remains; a large residual-factor beta alone is
not strong evidence. Exactly redundant factors still fail the rank check.
OLS/HAC intervals retain their existing conditional interpretation and are withheld
on irregular dates. They do not account for searching across many model choices.

For example, a sector return residualized against the market answers a different
attribution question from the sector's raw return. It is not a pure sector shock.
Do not interpret its coefficient or the changed market coefficient without the
declared controls and the remaining-variation statistic.

## Exposure stability

Scaling and residualization are fitted independently inside every rolling window.
Future observations cannot alter earlier rolling fits. Windows contain the configured
number of retained observations; where data is missing their calendar spans may differ.
Read each window's actual dates and the study's discontinuity diagnostics.

For each non-intercept coefficient, reports show window count, minimum, median,
maximum, last valid value and its window end date, sample standard deviation (only with two or more windows),
and positive/negative shares. The last valid value is marked stale if the newest eligible
window could not be fitted. Skipped window end dates are listed explicitly, including
when no rolling fit succeeds. Charts leave gaps rather than connecting across those
failures. Reports retain the configured window length and success/skip counts.
These are descriptive summaries of overlapping fits.
They are not independent tests, confidence bands, structural-break tests or forecasts.
No stability score or arbitrary pass/fail cutoff is imposed.

Compare different rolling-window settings to investigate horizon sensitivity.
Sign changes near zero may be economically negligible. Residualized control
coefficients can move partly because the estimated residualization basis changes
between windows. Original-factor comparisons help retain the economic context.

## Data discipline and limits

No new connectors or assumptions about commodity fields are introduced. Real
business-driver inputs need explicit transformations, units, timing and source
definitions from the normalized research-factor provider. Do not insert a futures
price as if it were an equity total-return index. Spot changes, rolled futures
returns and refining spreads describe different exposures.

The current tool explains contemporaneous returns. It does not forecast future
factor realizations or rank stocks cross-sectionally. PCA, ridge, predictive
walk-forward validation, transaction-cost analysis and cross-sectional signals
remain separate increments.

## Run the offline example

```sh
.venv/bin/python examples/factor_model_comparison.py
```

This generates synthetic monthly EUR stock and benchmark observations on the XETRA
calendar plus synthetic oil/gas percentage-point changes. It needs no network,
license or real security mapping. Replace the definitions and input panel to use
the identical estimator for healthcare, technology or any other sector.

The standalone Dash app offers line-based editors:
- Comparison: `Market and sector | market_excess,extra_0`
- Residualization: `extra_0 | market_excess,hml`

The IDs must exist in the selected full model. Comparison and residualization results,
remaining variation and rolling summaries appear below the fitted exposures.

## Reuse decision

Before implementation we checked
[statsmodels OLS](https://www.statsmodels.org/stable/generated/statsmodels.regression.linear_model.OLS.html),
[its rolling regression example](https://www.statsmodels.org/stable/examples/notebooks/generated/rolling_ls.html)
and [Alphalens Reloaded](https://github.com/stefan-jansen/alphalens-reloaded).
We retain statsmodels for regression and borrow the transparent comparative-reporting
workflow. Alphalens focuses on predictive cross-sectional signals; importing that
framework would not simplify this contemporaneous attribution increment.
No new dependency was added.

The small `factor_attribution.py` module contains numerical design scaling,
residualization, original-factor model comparisons and descriptive rolling summaries.
Provider normalization and main study orchestration remain in their existing modules.
