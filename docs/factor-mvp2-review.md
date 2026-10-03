# MVP 2A implementation review

Reviewed 2026-10-03 against the current uncommitted backend and sibling
FactorPlayground app. Review only: no application code changed in this pass.

## Assessment

The supported happy paths work, but this is still a preset-specific single-stock
MVP, not yet the flexible factor framework requested. The numerical library is a
sound choice and does not need replacing. Fix the provider boundary and coverage
issues, settle the gap-aware inference policy, and remove preset-specific
assumptions from the data/estimation contracts before building cross-sectional
research on top.

## Findings

### 1. Factor selection is hard-wired throughout the stack — high-priority design gap

`skills/factor_regression.py:42–96, 324–356`, `domain/models.py:316–336, 1229–1238`,
and `FactorPlayground/app.py:40–50` fix the choices and number of factors. Custom
means four benchmark instruments feeding the same three prescribed spreads. The
French branch always includes all six factors; there is one optional industry.
The response allows at most seven regressors plus intercept and the input models
contain fixed factor names. Adding two industries, omitting CMA or adding oil
requires edits to several modules, not a study configuration change.

Recommendation: presets should expand into a list of explicit factor definitions.
The estimator should accept one aligned target and a matrix of selected factors,
with the same ordered metadata driving coefficients, correlations, rolling output
and Dash labels. Start with asset-return and spread factors; add yield changes as
a separate typed transform when needed. No formula evaluator or runtime plugin
loader is required. Keep centralized resource bounds and sample/rank validation,
but decouple them from a particular preset's factor count.

### 2. Monthly FX requests omit the required opening endpoint — P2 correctness

`skills/factor_regression.py:253–255` requests FX from the nominal study start,
although monthly asset construction needs the preceding month-end. The Yahoo
adapter privately retrieves an extra 40 days, hiding the mistake. A replacement
provider that returns exactly its requested interval loses the first month.

Reproduction: synthetic monthly study 2017-01-01 through 2021-12-31, with a valid
EUR/USD adapter that respects the requested range. Result: 59 observations and
`insufficient_history` instead of a valid 60-month study. The earlier endpoint
is absent because the orchestrator never requested it.

Fix: derive the required FX range from the actual retained asset interval
endpoints and request that range explicitly. Verify adapter interchangeability
with a strict range-respecting fake. Do not require undocumented adapter padding.

### 3. Missing periods are compressed before HAC inference — P2 quantitative risk

`skills/factor_regression.py:324–372` passes only retained rows to HAC; their real
spacing is discarded. A missing month or several missing sessions becomes one
step in the covariance calculation. A diagnostic reports discontinuities but
ordinary 95% HAC intervals are still displayed.

Reproduction: preserve the same 300 numerical observations and put each daily
return on every second exchange session. The report records 299 gaps, but gives
exactly the same standard errors as the consecutive-session version. Its first
252-observation rolling fit spans 731 calendar days. The rolling behavior is
consistent with the documented observation-count setting; it is not a one-year
exposure window and should not be described as one.

The [statsmodels HAC documentation](https://www.statsmodels.org/stable/generated/statsmodels.stats.sandwich_covariance.cov_hac.html)
assumes consecutive equally spaced periods. This reproduction does not show that
OLS coefficients are wrong, nor establish the size or direction of confidence-
interval error. It shows that the configured lag is a retained-row lag, not the
chosen frequency's period lag. Irregular data need an explicit inference policy.

Fix: preserve the study calendar through estimation and validate a gap-aware
covariance approach, or withhold standard HAC intervals when its spacing
assumption is not met. Do not fill missing returns with zero to make the data
look complete. Show actual rolling start/end and coverage in the UI.

### 4. Monthly coverage losses can disappear from the report — P2 diagnostics

`skills/factor_data.py:71–76` drops an incomplete month; the regression computes
its original count only after this preprocessing. Missing leading/trailing months
need not produce an internal discontinuity either.

Reproduction: remove one daily observation from the first month of the synthetic
96-month French study. Output: 95 observations, **0 dropped, 0 gaps**, and no
missing-data diagnostic. The exclusion itself is appropriate; the coverage
accounting is incomplete. The existing implementation notes describe this narrow
counting definition, but the displayed summary is misleading for routine use.

Fix: preprocessing should return observations plus a small coverage summary:
expected complete periods, periods available, invalid/incomplete periods,
FX endpoint losses and cross-input alignment losses. Keep the reasons visible
without storing raw licensed observations in reports.

### 5. Frequency selection still requires daily asset inputs — P2 extensibility gap

`skills/factor_data.py:59–61` validates every asset interval against daily session
adjacency before branching to monthly aggregation. A correctly typed native
monthly asset return is accepted by `FactorReturnSeries` but then disappears
from a monthly study. A native 2022-12-30 to 2023-01-31 interval reproduced an
empty normalized output.

Fix: declare source frequency/interval convention in the normalized asset
contract. Validate native monthly observations directly; aggregate daily asset
returns only when necessary. Validate provider availability rather than pretending
all frequencies can be manufactured. Published long–short factors must retain
native frequency or a separately justified aggregation convention. A weekly
selector alone is not sufficient, especially for research spreads.

### 6. The research-factor contract is tied to Kenneth French — P2 extensibility gap

`domain/models.py:316–336` restricts source to `kenneth_french` or `fixture`, uses
fixed French columns and USD, while the regression encodes regional calendar
assumptions itself. A Bloomberg/internal research-factor adapter cannot satisfy
this supposedly normalized contract truthfully. Constructing the panel with
`source='bloomberg'` reproduces validation failure.

Fix: retain the approved provider identity vocabulary but use source-independent
named factor series with explicit units, type, currency and interval convention.
French column names, CSV parsing and source calendars should be adapter/catalogue
concerns. USD is correct for the French preset, not a universal factor-framework
constraint. The [French methodology](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_5developed.html)
confirms its developed-market returns are USD-based.

### 7. Unexpected Dash failures are difficult to diagnose — P2 maintainability

`FactorPlayground/app.py:894–927` catches all unexpected exceptions and replaces
them with one generic message. The app writes no diagnostic event for this path.
The backend does expose coarse failure categories, but UI construction/callback
failures have no stage or error identifier. Adding a factor absent from the fixed
label dictionary is one example that reaches this path.

Fix: return a safe structured failure code and stage (fetch, normalize, align,
fit, render), with a correlation ID. Record safe exception type/code metadata;
never dump vendor payloads, exception locals, secrets or raw financial inputs.
Keep the user message brief but distinguish invalid settings, provider failure
and an application defect.

## Minimal architecture recommendation

Use a single pipeline:

1. A study config selects a preset or explicit factor list, frequency, window and
   study currency. Presets are ordinary catalogue entries, not estimator branches.
2. Adapters return typed observations with source conventions and interval metadata.
3. Shared preparation validates/aggregates/converts/aligns and returns the matrix,
   ordered factor metadata and coverage diagnostics.
4. One statsmodels fitting function handles the matrix. Rolling fits call the
   same numerical helper with historical slices.
5. Report and Dash rendering use returned metadata rather than duplicate lists.

Split the long `analyze()` method along these existing responsibilities, not into
an elaborate class hierarchy. Keep connection settings separate from statistical
settings. Small immutable catalogue definitions and parameter objects are enough;
there is no demonstrated need for a plugin framework, distributed compute or a
new database. Download caching is a later measured optimization, with explicit
source version/refresh behavior because French files can be revised.

“Remove all hard coding” should mean removing scattered business assumptions,
not removing validation, safe endpoint mappings, documented financial conventions
or library pins. The number of requested factors should follow the selected list;
resource limits should be centralized and configurable, and insufficient sample
size/rank deficiency must still prevent invalid fits.

## What passed and limits of this review

- Reran 59 factor backend/provider tests, 10 standalone Dash tests, Ruff and mypy:
  all passed. The previous delivery's full-suite/build results are not counted as
  newly rerun full-suite results in this review.
- Existing tests cover known daily/monthly exposures, subtracting cash once,
  industry incremental fit, future-data perturbation of rolling fits, missing
  intervals, calendar holidays, FX endpoint conversion/DST, source parsing and
  transport parity/no raw-history export.
- Additional temporary offline experiments reproduced findings 1–6. These were
  diagnostic scripts, not changes to the production test suite or claimed fixes.
- No new live Bloomberg/entitlement check or claim of forecast validation was made.
- Common happy-path tests mostly use independent synthetic regressors and the
  same fixed model definitions. Passing them does not test new factor selection,
  arbitrary native-frequency sources or the strict FX provider boundary.

Recommended order: fix endpoint requests and coverage reporting, settle missing-
period inference, then generalize factor definitions/contracts and make Dash
consume that metadata. Cross-sectional MVP 2B should follow these corrections.

## Remediation

The findings above are addressed by the configurable v3 factor contract, native
monthly normalization, explicit FX endpoint requests, per-input coverage accounting,
withheld HAC inference on irregular samples, modular estimation, and the updated
standalone Dash controls and safe error references. See factor-mvp2-implementation.md
and tests/test_factor_review_fixes.py for the current behavior and regressions.
