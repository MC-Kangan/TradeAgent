# Review of the SX5E daily factor attribution prototype

Reviewed 7 October 2026 against the supplied `samplefactor.txt` (707 lines).
This was a source-code review, not a live Bloomberg reconciliation. The external
`significance_test.py`, cached beta file and actual vendor observations were not
provided. Findings below distinguish visible implementation defects from modelling
choices that require validation.

## Shareable summary

The dashboard is a useful prototype, particularly its stock-by-factor view. However,
the current daily attribution should not yet be used to draw investment conclusions.
The most important issue is that the regression estimates exposures to historical
Fama–French factors, while the daily calculation applies those exposures to different
ETF/index proxies. A beta is specific to the factor on which it was estimated; similar
labels do not make two return series interchangeable. Yield factors also change units
between estimation and attribution, missing quotes are silently treated as zero moves,
and the refresh buttons do not actually update the underlying calculations. These are
implementation issues that should be corrected before further interpretation.

The underlying OLS regression is a reasonable starting point, and lack of
orthogonalization is not itself an error. Confidence in its exposures still requires
explicit return/currency conventions, a sufficient sample, collinearity and stability
checks, appropriate uncertainty estimates, and independent validation of factor
selection. Orthogonalization cannot repair inconsistent inputs or selection bias.

## Confirmed implementation issues

| Finding | Evidence in the supplied file | Consequence and correction |
|---|---|---|
| Different factors in estimation and daily attribution | `BASE_FACTOR_PROXIES`, lines 35–42; `compute_betas`, lines 329–354; `get_daily_factor_moves`, lines 396–435 | French SMB/HML/RMW/CMA betas are applied to Bloomberg proxy returns or spreads. Momentum is fitted to French momentum but applied to a long-only ETF. Fit on the exact proxy history if those proxies are used for monitoring, or use the published French observations consistently and disclose their publication lag. |
| Yield units change between stages | `_extra_series`, lines 265–281; yield branch, lines 424–428 | Historical EUR10Y uses the absolute difference of yield levels, but daily attribution uses percentage change divided by 100. For a quoted yield moving from 2.0 to 2.1, the historical transform is 0.1 percentage point (10 bp); the daily code supplies 0.05. Use the same absolute-change transform and declared units at both stages. |
| Missing quotes become zero | `chg`, lines 409–411; extra-factor handling, lines 421–423 | An unavailable factor looks unchanged, distorting explained return and the remainder. Return an explicit unavailable/stale status; do not manufacture a zero. |
| Refresh controls do not refresh data | UI/callbacks, lines 580–667 | Refresh Betas has no callback. Refresh Prices enables a timer, but no callback consumes its ticks to query prices or update attribution. Wire both operations through actual data retrieval and verify that data timestamps and displayed results update. |
| Model intercept is discarded | Cached coefficients, lines 363–365; attribution, lines 467–478 | The daily model total excludes the fitted intercept. The remainder therefore includes this omitted term as well as unexplained return. Preserve the intercept and state the convention. Do not directly apply a monthly intercept as if it were daily. |
| Cache does not identify the fitted specification | `load_or_compute_betas`, lines 375–390 | A recent cache with any extra factors is accepted even if tickers, selected factors or transforms changed. Key the cache by the complete model specification and estimation cutoff, or omit caching until necessary. |
| Certificate validation disabled | French downloads, lines 217–219 and 243–245 | Both downloads turn off TLS verification. Retain certificate validation and use the firm's approved CA/proxy configuration. |

## Regression and data assumptions requiring validation

1. **Monthly-to-daily transfer is untested.** The history request is monthly, while
   the overlay applies betas to daily or intraday changes. Such a mapping may be
   useful as an approximation, but monthly exposures and R² do not establish daily
   attribution quality. Prefer daily estimation for a daily monitor, or validate and
   clearly label the frequency transfer. Fit strictly before the displayed period.

2. **Small samples and factor selection.** The script permits 24 observations for
   seven base factors plus extras and an intercept. That leaves little information
   per estimated parameter. The hard-coded “confirmed” extras reference a separate
   significance test we could not inspect. Selection on the same sample can make
   reported p-values optimistic. Preserve the selection procedure and sample dates;
   validate on a later period and address multiple testing when screening many ideas.

3. **Standard errors and collinearity.** The script calls `sm.OLS(...).fit()` with
   default non-robust covariance. For return time series, inspect residual serial
   dependence and heteroskedasticity and choose uncertainty estimates accordingly;
   HAC is an available option, not a cure for misspecification. Add rank checks,
   correlation/VIF diagnostics and beta stability checks. A pseudoinverse can produce
   coefficients even when redundant columns prevent unique identification.
   [Statsmodels OLS documentation](https://www.statsmodels.org/stable/generated/statsmodels.regression.linear_model.OLS.fit.html)

4. **Currency, cash and dividend conventions need to be explicit.** French developed-
   market factors are expressed in USD and include dividends. The script regresses
   local stock price returns against a local index, French style spreads and other
   drivers, without an explicit currency-normalization or risk-free-return policy.
   A hybrid explanatory regression is not automatically invalid, but it is not a
   standard French excess-return model. Its intercept should not be called alpha.
   The PX_LAST history request does not explicitly specify corporate-action
   adjustment flags, so adjustment behaviour needs verification in the actual
   Bloomberg environment rather than an assumption that prices are adjusted or
   unadjusted. [French factor methodology](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_5developed.html)

5. **Observation timing and economic interpretation.** Daily quote timestamps are
   not checked for a common observation window. Missing history is dropped without
   a coverage report; previous-value filling can obscure stale observations. Generic
   front-month futures need a documented roll convention because contract switches
   may create movements unrelated to the intended commodity exposure. A country's
   domestic index is a reasonable starting control, but country of risk alone may
   not capture a multinational company's actual business exposure.

## Orthogonalization

OLS does not require mutually uncorrelated factors. Strong correlation increases
uncertainty and can make individual coefficients unstable; exact redundancy prevents
unique identification. Sequential orthogonalization can make an attribution basis
clearer, but the allocation depends on factor ordering. Ranking the order by in-sample
significance adds selection risk. PCA provides orthogonal statistical combinations,
but those combinations are less directly interpretable and dropping components changes
the model. Neither method repairs mismatched units, different live proxies or
unvalidated data. Start with consistent inputs and diagnostics; use transformations
only for a clearly stated interpretive purpose.

## Suggested acceptance checks before resuming interpretation

- For every factor, confirm identical identifier, field, transform, currency, units
  and observation window in estimation and monitoring.
- Reconcile a small set of stock and factor moves against Bloomberg, including a
  dividend/split date, a missing quote, a holiday and a futures roll boundary.
- Verify the yield-change example above and a synthetic model with known betas.
- Check that intercept plus contributions plus unexplained return equals the actual
  return under the stated currency/cash convention.
- Change the model settings and confirm the cache is invalidated; test that both
  refresh controls fetch fresh data and surface failures rather than stale success.
- Validate exposures and selection on a later time period. Treat the unexplained
  remainder as model error plus omitted effects, not proof of company-specific news.

The recommendation is to pause reliance on the current daily contribution and
significance claims until these checks pass, while retaining the useful dashboard
workflow and the standard OLS foundation.
