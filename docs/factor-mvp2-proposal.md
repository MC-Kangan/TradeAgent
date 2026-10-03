# Factor research MVP 2 — proposal

Status: staged design recommendation, prepared 2026-10-03. MVP 2A is now implemented;
see [implementation notes](factor-mvp2-implementation.md). MVP 2B remains planned.

The next release should improve economic relevance, data comparability and
stability before increasing the number of regressors. Deliver two working
increments: 2A improves single-stock explanation; 2B evaluates cross-sectional
signals. Keep the standalone Dash app as the research interface and keep all
calculations in TradeAgent. VibeResearch integration can consume the resulting
bounded reports later.

## What the current model can and cannot establish

MVP 1 explains historical stock total returns using a market ETF, growth-minus-
value ETFs and momentum-minus-market ETFs. These are understandable proxies,
not pure academic factor portfolios. Subtracting the market with coefficient
one does not make a factor market-neutral or orthogonal. A high in-sample R²
is neither evidence of forecasting skill nor proof of causation. HAC changes
uncertainty estimates; it does not repair omitted variables, structural breaks,
poor return conventions or selection bias.

## 2A: economically relevant single-stock research

Replace the fixed three-column construction with a bounded selection of named,
versioned factor definitions. Start with market, the existing style and momentum
spreads, and one user-selected regional industry factor. Compare this four-factor
model with the existing three-factor model on exactly the same observations.
Quality is the next optional style addition, not a mandatory fifth factor.
Use a modest application cap, for example eight factors; this is an operational
limit, not a statistical claim that eight is always safe.

### Candidate factor catalogue

| Family | Candidate input | Interpretation / caution |
|---|---|---|
| Market | Consistent regional total-return benchmark | Broad equity sensitivity; preferably excess return when an appropriate cash-return series exists. |
| Industry | Regional pharma, biotech, banks, semiconductors, energy, etc. | Industry conditions beyond broad market movements. Use one justified industry proxy initially. |
| Styles | Size, value, momentum, profitability/quality, investment, low volatility | Factor definitions differ; long-only fund-minus-market proxies are not interchangeable with academic long-short portfolios. |
| Rates | Change in a relevant government yield, in basis points | Discount-rate sensitivity. A yield change is not a bond total return or a risk-free accrual. |
| FX | Explicitly directed currency return | Economic currency exposure, distinct from converting a listing's returns into the study currency. |
| Commodities | Defined oil/energy/metal total-return series or carefully specified price changes | Spot prices, futures indexes and producer equities represent different exposures. Futures require roll conventions; negative prices need an appropriate transformation. |
| Credit / liquidity | Change in a documented spread or an entitled liquidity series | Financing sensitivity; likely correlated with other macro inputs. Defer until a relevant study needs it. |
| Company events | Timestamped earnings, drug-trial or regulatory events | Event annotation and sensitivity analysis first; sparse jumps should not be forced into a generic daily factor. |

For U.S. pharmaceuticals, IHE is a potential fund proxy; for biotech, IBB or XBI
may be relevant. XBI's equal-weighted industry construction can introduce a
different size mix, so these are alternatives rather than interchangeable names.
For Europe, candidate index families include MSCI Europe Pharmaceuticals,
Biotechnology and Life Sciences and STOXX Europe 600 Health Care. The latter is
broader than pharma. Actual total-return variant, currency, entitlement, field
and Bloomberg identifier must be verified before use. Do not put U.S. fund
returns beside a local-currency European stock without an explicit study basis.

Check the target stock's weight in its industry benchmark. Otherwise the
regression can partly explain the stock with itself. Flag material self-inclusion;
construct a leave-one-out benchmark only when dated constituent weights exist.
Do not subtract today's weight from the whole historical series.

Sources: [IHE factsheet](https://www.ishares.com/us/literature/fact-sheet/ihe-ishares-u-s-pharmaceuticals-etf-fund-fact-sheet-en-us.pdf),
[IBB](https://www.ishares.com/us/products/239699/ishares-us-biotechnology-etf),
[XBI](https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-biotech-etf-xbi),
[MSCI Europe pharma industry](https://www.msci.com/www/index-factsheets/msci-europe-pharmaceuticals/0880349158),
[STOXX Europe healthcare](https://stoxx.com/index/sxdv/?factsheet=true).

### Alternative academic baseline

Offer a separate research preset when the input adapter is ready: Fama–French
market, size (SMB), value (HML), profitability (RMW), investment (CMA), plus
momentum. Do not append duplicate ETF versions of the same concepts by default.
The current growth-minus-value coefficient has the opposite orientation from
value-minus-growth, and different portfolio construction; it cannot simply be
renamed HML. Profitability is narrower than a general quality definition.

French's developed-region factors, including Europe, are USD returns and use a
U.S. cash benchmark. European local-currency equity returns need explicit USD
conversion for that preset. Alternatively use verified local-currency research
inputs. Parse percent versus decimal units, missing sentinels, publication lag
and data version explicitly. Published research factors are useful for historical
explanation but are not necessarily available at the time a trading signal is
formed. Preserve the source vintage; historical methodology/data revisions occur.

Sources: [five-factor definitions](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_5developed.html),
[momentum definitions](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_developed_mom.html),
[data library and revision notes](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html),
[MSCI style families](https://www.msci.com/indexes/category/factor-indexes),
[AQR quality-minus-junk](https://www.aqr.com/Insights/Datasets/Quality-Minus-Junk-Factors-Daily).

### Preparation and assumptions

1. **Study currency and return convention.** Make these visible controls.
   Validate splits, dividends, duplicate dates, stale observations and vendor
   differences. Never infer dividend inclusion merely from an adjusted-price
   field or silently mix gross and net conventions.
2. **Cash return where available.** Regress stock-minus-cash on market-minus-cash
   for an excess-return study. Match currency, interval and compounding. A quoted
   annual yield must be converted with its documented day-count convention;
   dividing every quote by 252 is not a universal conversion. Do not subtract cash
   again from an already excess-return factor or a zero-cost long-short spread.
   Without an appropriate cash series, keep an explicit raw-return study and do
   not label its intercept risk-adjusted alpha. Even a valid alpha is model-relative.
3. **Dates and trading sessions.** Add exchange-calendar validation for selected
   supported venues. Distinguish genuine missing sessions from holiday closures,
   including long Easter weekends currently excluded by the four-calendar-day
   heuristic. Daily remains useful; add a weekly sensitivity view with explicit
   endpoints and geometric aggregation of asset returns. Form each period's
   factor spread from its component period returns; do not compound a daily
   long-short spread as if it were an ordinary funded asset without specifying
   its capital/rebalancing convention. Weekly aggregation does not eliminate
   non-synchronous-close issues.
4. **Outliers and events.** Show the largest residuals and influential observations.
   Verify suspected errors; retain real market jumps. For pharma, a drug-trial
   failure can be economically central. Never quietly winsorize it away.
5. **Historical availability.** Price-based explanation uses contemporaneous
   factor realizations. Predictive studies must use only information available
   before their label window. Fundamentals need publication dates; macro data
   need release timestamps and vintages. Do not expand into these datasets before
   their availability contracts work.

The current FactorReturnSeries contract is suitable for asset returns, not
arbitrary factors. A zero-cost spread, yield change and characteristic need
explicit kinds and units, with appropriate validation. Add only kinds consumed
by the next increment; keep vendor parsing inside adapters. Keep return processing
independent of the regression solver.

### Stability and correlated factors

Keep OLS/HAC as the baseline. Add a trailing 252-session coefficient plot,
refitted monthly, with sample/coverage warnings. An optional 504-session comparison
shows window sensitivity. Also report residual autocorrelation, influential dates
and incremental fit from the industry factor. More precise-looking standard
errors are not a substitute for a stable specification.

First avoid redundant factors. Then optionally regress an industry return on the
chosen baseline and use its residual as the incremental industry factor. That
residual is orthogonal to the baseline in the fitting sample only; the procedure
changes coefficient attribution and does not create a pure economic cause.
With the baseline retained, this reparameterization does not itself improve OLS
fitted values. Fit every transformation on the training window, then freeze it
for evaluation.

Ridge is a later comparison when correlated factors produce unstable estimates;
standardize using training statistics and choose regularization through
chronological validation. Do not present ordinary OLS confidence intervals for
ridge estimates. PCA should remain optional and later: it preserves directions
of factor variance, not necessarily economically meaningful or predictive
signals. Components can be difficult to name and change across windows.

Sources: [statsmodels rolling regression](https://www.statsmodels.org/stable/examples/notebooks/generated/rolling_ls.html),
[scikit-learn ridge](https://scikit-learn.org/stable/modules/linear_model.html#ridge-regression-and-classification),
[PCA](https://scikit-learn.org/stable/modules/decomposition.html#pca).

Compare baseline and expanded models on an identical sample, using both training
fit and later-period residual error/coefficient stability. If later realized
factor returns are used, call the exercise **held-out explanatory fit**, not
next-period forecasting. Track specification trials; no automated hunt for the
highest in-sample R². Any true prediction task needs a separately lagged design.

## 2B: cross-sectional signal evaluation

Start with a bounded watchlist of approximately 50–200 liquid stocks and one
region at a time. The count is a manageable starting scope, not a guarantee of
statistical power. Support U.S. and European runs separately before pooling.
Research on a current watchlist is exploratory: historical investability claims
require dated membership, delisted securities, terminal returns and historical
liquidity eligibility. Do not silently backtest today's survivors as a historical
index universe.

Use three price-based signals before introducing fundamental data:

- **12–1 momentum:** total return over the preceding year excluding the latest month.
- **Low volatility:** negative trailing 63-session realized volatility (higher score = lower volatility).
- **Short-term reversal:** negative trailing 21-session total return.

Calculate each independently. Apply same-date ranks, show raw results and
within-sector ranks separately, and record coverage and exclusions. For Europe,
country/currency composition also needs attention; do not fit too many controls
in tiny groups. Delay value, profitability and earnings revisions until their
point-in-time source data are available. Pharma company comparisons may later
need cash runway and pipeline-aware definitions rather than generic earnings
multiples for loss-making biotech companies.

For the first evaluator, use non-overlapping monthly formation dates and a
single next-month horizon. Scores based on a completed month-end close are not
assumed tradable at that same close. Specify entry at the next session close and
exit at the corresponding next rebalance close, with corporate-action-consistent
returns over that holding interval. Skip an unavailable entry rather than
forward-filling an executable price. For multi-market Europe runs, specify an
eligible common formation cut-off and each listing's actual entry/exit session.

Report rank IC (Spearman correlation between scores and subsequent returns),
quintile average returns, monotonicity, coverage, turnover and period-by-period
stability. Show top-quintile versus equal-weight eligible-universe returns for
personal-investing relevance. A top-minus-bottom spread remains a diagnostic;
do not call it a tradable short strategy without borrow, funding and cost rules.
Add explicit illustrative trading-cost sensitivity, not a universal cost estimate.

Assess uncertainty across dates with suitable dependence-aware errors or a date-
block bootstrap. Stocks sharing a date are not independent repetitions. Use
chronological held-out periods, fit transforms on past data, and keep all names
from a date in the same fold. Purge label windows crossing a train/test boundary.
No random row-level splitting and no compounding overlapping monthly labels as
if each were a separately financed daily return.

A common pharma index return is identical for all pharma names on a given date;
it cannot rank them by itself. A stock's past momentum, valuation, earnings
revision or previously estimated industry beta can vary across that cross
section. Distinguish this predictive evaluation from a contemporaneous
cross-sectional risk model that estimates factor returns from stock exposures.
Fama–MacBeth regression can follow once the simple ranking evaluator is trusted.

## Reuse and implementation boundaries

Borrow the date/asset panel, forward-return alignment, IC, quantile and turnover
ideas from [Alphalens Reloaded](https://github.com/stefan-jansen/alphalens-reloaded)
and its [performance implementation](https://raw.githubusercontent.com/stefan-jansen/alphalens-reloaded/main/src/alphalens/performance.py).
Use [Vibe-Trading's factor core](https://raw.githubusercontent.com/HKUDS/Vibe-Trading/main/agent/src/factors/factor_analysis_core.py)
as an additional reference for aligned factor/return masks. These are ideas to
adapt and numerically test, not whole-platform dependencies or permission to
copy code without license attribution. Qlib's broad workflow architecture is
useful background but unnecessary for this MVP. NumPy/pandas/SciPy and statsmodels
already cover the first evaluator's numerical needs.

2A requires a bounded selected-factor list and preparation metadata; 2B adds a
bounded dated universe/panel contract. Do not overload the existing portfolio
basket request or create a universal plugin system. Start with one factor
calculation and one panel evaluator, registered in the existing fixed skill
registry. Dash remains a caller. Add two views: **Explain a stock** and
**Test a signal across stocks**. Defer new databases, automatic factor discovery,
large alpha libraries and VibeResearch integration until a working need exists.

## Proposed acceptance gates

- 2A: synthetic industry exposure is recovered; redundant factors are diagnosed;
  raw versus excess-return conventions are correct; holidays/missingness remain
  distinct; currency/basis mismatches fail; baseline comparisons share identical
  observations; changing future data cannot alter earlier rolling estimates or
  fitted transformations; raw licensed data do not enter exports.
- 2B: synthetic positive, negative and null signals produce expected IC and
  quantile ordering; tied scores/constant groups/insufficient coverage are explicit;
  future data cannot change historical scores; membership and terminal-return
  cases remain represented; entry timing and turnover/cost arithmetic are tested.
- Both: native/HTTP/MCP produce the same typed findings; Dash labels explanatory
  versus predictive outputs clearly; existing quality gates stay green.

Suggested implementation order: 2A factor selection + industry + coverage, then
2A rolling/comparison and valid excess-return support, then the 2B price-signal
evaluator. Academic presets, automated FX conversion, ridge/PCA, fundamental
panels and firm-specific event data are separate additions with their own tests.
