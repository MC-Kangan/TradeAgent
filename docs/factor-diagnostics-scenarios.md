# Factor diagnostics and scenario sensitivity

This increment adds diagnostics to the existing single-stock historical attribution
model without changing its reference estimator. The reference remains joint OLS with
HAC covariance on the aligned factor matrix. Report schema is
`factor-regression-v5`.

## What was already available

Before this increment, the backend already reported adjusted R-squared, standardized
effects (`beta × factor volatility / stock volatility`), staged missing-data coverage,
and original/selected-basis correlation matrices. The Dash app already rendered those
outputs, including side-by-side factor heatmaps after residualization or sequential
attribution. These capabilities were retained rather than reimplemented.

## New outputs

Each original factor now has an intercept-inclusive univariate beta and univariate
R-squared beside the original joint-model beta. A large difference is a specification
sensitivity clue, usually caused by shared factor variation; it is not evidence that
one estimate is automatically correct. Partial correlation and same-sample incremental
R-squared remain the stronger marginal-information diagnostics.

The full model reports a HAC covariance-based joint F test of the null that all factor
slopes are zero. It establishes joint in-sample association, not economic materiality,
causality or out-of-sample forecasting power.

Residual diagnostics use the same fitted residuals:

- Durbin-Watson and Ljung-Box inspect serial dependence.
- Jarque-Bera summarizes skew/kurtosis departure from normality.
- Engle ARCH LM inspects volatility clustering.

The configured HAC lag is used, bounded to 1–10 and no more than one fifth of the
sample. Lag-based tests are withheld when retained intervals are irregular because
adjacent rows then do not represent consistent time steps. Jarque-Bera remains
available. Small p-values are review flags; the app does not turn them into automatic
factor selection or a claim that GARCH is required. HAC inference is asymptotic and
does not require exactly Gaussian residuals.

The Dash scenario panel accepts one hypothetical move per original economic factor and
calculates `original joint beta × move × 10,000` basis points. Return factors use decimal
moves, so `0.01` means 1%; price-change factors use their declared units. It deliberately
keeps the original factor basis when an orthogonalized attribution view is selected,
because users normally stress observable market moves rather than statistical residual
shocks. The panel excludes the intercept and residual and therefore shows factor
sensitivity, not a return forecast or a reconciled P&L attribution. No data is fetched
and no new model is fitted when a scenario value changes.

## Deliberate exclusions

Iterative p-value deletion is not included. Repeatedly selecting and refitting on the
same sample creates post-selection bias, unstable models and invalid naive p-values.
A future exploratory selector would need a predeclared hierarchy, a visible selection
path and chronological validation; it must not replace the economically specified
reference model.

PCA is a reasonable next diagnostic for the standardized factor matrix. It should show
explained variance and loadings while retaining the economic-factor regression as the
reference. Principal-component signs are arbitrary and components can be unstable
across samples, so PCA should not silently rename latent components as investable risks.

Vasicek or Blume beta adjustment is deferred until cross-sectional exposures can supply
a point-in-time prior and out-of-sample validation. A single stock does not provide a
defensible cross-sectional shrinkage target. Classical Chow testing is also deferred;
a HAC interaction test around a predeclared break date is more consistent with the
current error assumptions.

Geometric regression attribution is not treated as a quick correction. Independently
compounding factor contributions generally does not reconcile to the stock return
because interaction and residual terms need an explicit linking rule. If required, a
future linked-attribution design should state how those interaction effects are
allocated rather than calling a Brinson portfolio method directly equivalent.

## Reuse decision

The implementation uses the project's pinned statsmodels dependency. Its documented
regression results provide robust Wald/F contrasts, and its statistics module provides
Durbin-Watson, Ljung-Box, Jarque-Bera and ARCH LM primitives. No solver or test is
reimplemented and no dependency is added. Dash's core editable DataTable is deprecated;
the scenario uses bounded native numeric inputs instead of adding a grid package for a
small table.

- [statsmodels regression results](https://www.statsmodels.org/stable/generated/statsmodels.regression.linear_model.RegressionResults.html)
- [statsmodels statistics and residual diagnostics](https://www.statsmodels.org/stable/stats.html)
- [statsmodels regression-diagnostics example](https://www.statsmodels.org/stable/examples/notebooks/generated/regression_diagnostics.html)
- [Dash editable table notice](https://dash.plotly.com/datatable/editable)

Focused tests compare univariate results with independent statsmodels fits, verify the
joint test and residual output contract, withhold lag tests on irregular data, and
check scenario arithmetic in mixed factor units.
