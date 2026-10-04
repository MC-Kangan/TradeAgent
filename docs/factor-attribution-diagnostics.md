# Single-stock attribution and relationships

The reference model remains joint OLS on the original normalized inputs. Two optional
changes of basis support interpretation:

- Explicit controls: regress each selected target factor on its specified original
  controls and an intercept, retaining those controls in the stock regression.
- Sequential: supply every selected factor ID exactly once in `sequential_order`.
  Each factor after the first is residualized on all preceding original factors
  and an intercept. The stock is then fitted jointly to the transformed matrix.

The sequential projections span the same preceding space as sequential QR/Gram–Schmidt,
but use the existing statsmodels OLS primitive. Original controls are used simultaneously,
not a chain of regressions of the stock residual on unadjusted factors. The explicit
control rules and sequential order are mutually exclusive. Full-sample rank checking
occurs before transformation; exact dependencies are rejected. Keeping a full-rank
basis preserves fitted returns and R². Order allocates shared variation and changes
coefficient meaning. Orthogonality is sample-specific and does not establish causality.

`FactorRegressionPresentation` includes attribution mode/order and:

- Original and selected-basis Pearson factor matrices, with original-model VIF/condition
  available in the full-model comparison and selected-basis diagnostics retained.
- Stock–factor Pearson and Spearman correlations on the same fitted observations.
- Partial Pearson correlation between stock and factor residuals after controlling for
  every other original factor. Undefined correlations are null, never zero.
- Incremental R² = full-model R² minus R² after removing that factor, with an intercept
  and identical observations retained. Shared information means increments need not sum.
- Rolling original stock–factor Pearson/Spearman correlations using the existing window
  length and month-end sampling schedule. Windows count aligned observations, so gaps
  can lengthen their calendar spans. No significance tests are attached to overlapping
  windows. Transformations are refitted within each rolling coefficient window.

The stock series is exactly the regression target (excess return when cash is subtracted).
These diagnostics concern configured returns/changes, never raw trending price levels.
PCA, ridge and significance-driven ordering are intentionally outside this slice.

The Dash controls use factor IDs from the active pack and manual definitions; no sector
or commodity names are embedded in the algorithms. Updated choices remove stale IDs.
Original mode ignores hidden transformation settings. Sequential mode requires a complete
visible ordering. Original coefficients remain available in the model comparison section.

## Ideas borrowed and checks

Reuse the existing [statsmodels partial-regression principle](https://www.statsmodels.org/stable/generated/statsmodels.graphics.regressionplots.plot_partregress.html)
and OLS solver, SciPy rank handling (already used by cross-sectional analytics), and Dash
non-searchable dropdowns. No research framework or new dependency is required.

Tests compare partial correlation and incremental R² against independent reduced/full
regressions, verify orthogonality and preservation of fitted values, check the one-factor
and tied-rank cases, and verify that changing future observations cannot alter earlier
rolling estimates/correlations. All original-factor diagnostics stay identical across
sequential orders. Licensed series remain transient and are not added to reports.
