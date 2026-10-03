# Backend review — U.S./European equity research and factor readiness

Reviewed 3 October 2026 at commit `a60e107`. Companion: [factor framework plan](factor-framework-plan.md).

## Recommendation

Keep the existing application, provider protocols, immutable analyst registry, and shared report envelope. They are a good foundation for a small research platform. Correct the data and execution discrepancies below before adding regression. Add two coherent research workflows rather than many individual factor “skills.”

The backend supports useful single-instrument analytics today. It does **not** yet provide a historical cross-sectional factor platform: no dated universe, point-in-time fundamental panel, factor-return catalogue, or regression implementation exists. Existing uses of “factor” often mean an individual accounting ratio or technical indicator, not an estimated statistical factor exposure.

This was a repository-wide architecture/interface/provider review, call-site inspection, targeted numerical and behavioral probes, and execution of the existing quality checks. It was not a live Bloomberg entitlement test, production usage audit, exhaustive proof of every strategy, or penetration test. “No production call sites found” below means within this repository; external Python clients were not inspected.

## Verification

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | 572 passed, 36.78 seconds |
| `.venv/bin/ruff check .` | Passed |
| `.venv/bin/mypy` | Passed, 40 source files |
| `.venv/bin/python -m build --no-isolation` | sdist and wheel built |
| `docker compose config --quiet` | Passed; no services started |
| CI's additional skill-documentation path check | **Failed when reproduced locally:** six missing paths |
| Targeted synthetic probes | Confirmed raw Yahoo close selection, queue parameter loss, absent HTTP-status retries, cross-market alignment failure, order-sensitive SEC selection, and ignored parameter typo |

No application code, dependencies, or settings were changed. No live financial data or credentials were needed for these probes.

## Findings, ordered by impact

### 1. P1 — Yahoo return adjustment is misrepresented

**Evidence:** [remote.py](../src/trade_research/providers/remote.py), `YahooPriceProvider`, lines 137–223. Its docstring promises split/dividend-adjusted closes, but parsing uses `indicators.quote[0].close` and never reads `indicators.adjclose`. Remote price provenance also omits adjustment, currency, and session-boundary metadata that inline data can supply.

**Reproduction:** a synthetic response with quote close `100` and adjusted close `90` returns `100.0`.

**Impact:** dividend-sensitive return studies can be presented as total-return studies when they are not. Vendor adjustment differences can contaminate regression, momentum, and comparisons. Do not infer exact corporate-action treatment from an unnamed `close` field.

**Fix:** define explicit raw execution OHLC, split-adjusted prices, and total-return series semantics; require providers to label what they supply. Never substitute adjusted close into otherwise raw OHLC bars. Add dividend/split fixtures and cross-provider contract tests before changing calculations.

### 2. P1 — Durable research silently loses instrument parameters

**Evidence:** [storage/runs.py](../src/trade_research/storage/runs.py), `PersistedAnalysisRequest.from_request`, lines 28–44; [application.py](../src/trade_research/application.py), `start_research`; [queue.py](../src/trade_research/queue.py), worker reconstruction.

Only portfolio `lookback`/`method` parameters are retained. Instrument parameters are discarded even after `start_research` validates them.

**Reproduction:** a technical request with `window=60` becomes a reconstructed request with no parameters and runs with `window=20`. Different parameterizations under the same request UUID can also collapse to the same persisted input, undermining conflict detection.

**Fix:** persist a typed, per-skill allowlist of normalized safe parameters, or reject requests whose parameters cannot be preserved. Do not start allowing arbitrary dictionaries or raw series into the queue. Require immediate/queued equivalence tests.

### 3. P1 — Cross-market portfolio alignment and currency conventions are incomplete

**Evidence:** [skills/portfolio.py](../src/trade_research/skills/portfolio.py), `_portfolio_data`, lines 220–282. It intersects exact `observed_at` datetimes, then computes returns between retained observations and annualizes as daily. It does not read quote currency or adjustment metadata.

**Reproduction:** two valid histories with the same 30 dates, one stamped at 08:00 UTC and the other at 14:00 UTC, produce `partial: Fewer than 21 aligned daily closes remain`.

**Impact:** European/U.S. sessions and differently stamped providers may not align at all. Dropped dates can turn some “daily” returns into multi-session returns. Cross-currency allocation volatility has no defined investor currency. A local-return correlation study is valid if clearly labeled; interpreting it as a base-currency portfolio is not.

**Fix:** explicitly model session date, observation time, availability time, return interval, and currency. Use exchange calendars and a documented common cutoff. Preserve missingness and coverage diagnostics. Start with same-region studies and reject base-currency allocation without FX inputs. Merely truncating timestamps to dates does not prevent using a U.S. close that occurs after a European decision time.

### 4. P1 — European identifiers are ambiguous across providers

**Evidence:** [providers/remote.py](../src/trade_research/providers/remote.py), suffix tables and `resolve_provider_symbol`, lines 33–94; [domain/models.py](../src/trade_research/domain/models.py), `InstrumentId`.

`EURONEXT` maps to `.PA` for Yahoo and `NA Equity` for Bloomberg. `EU` is also mapped to one vendor suffix. Neither identifies a unique European venue. Suffixes are appended even if a symbol already includes one.

**Reproduction:** `ASML / EURONEXT` becomes `ASML.PA` versus `ASML NA Equity`; `VOD.L / LSE` becomes `VOD.L.L` on Yahoo.

**Fix:** use precise venue identifiers and explicit provider mappings, with currency and share-class identity. Reject ambiguous `EU`/`EURONEXT` inputs rather than silently guessing. Add representative London, Paris, Amsterdam, Frankfurt, Milan, Madrid, and Swiss listing fixtures. Scope Nordic expansion explicitly rather than claiming all-European coverage.

### 5. P1 — SEC period selection can select a comparative as the current period

**Evidence:** [providers/sec.py](../src/trade_research/providers/sec.py), `_select_periods`, lines 370–401. Selection sorts by `fy` and `filed`; it does not resolve actual period end/start/duration or competing comparative observations. FY/Q4 contexts are both treated as annual.

**Reproduction:** two facts with the same FY, form, and filing date but ends `2024-12-31` and `2023-12-31` change the selected “current” period when their input order is reversed.

**Impact:** growth and ratios may depend on payload ordering. Separately, current/prior snapshots collected today cannot support historical backtests: there is no query-time availability cutoff or revision history. The adapter only reads `us-gaap` USD facts, so it is not a general European fundamental connector.

**Fix:** select by accounting context, actual period, duration, and filing availability; explicitly handle restatements and instant versus duration facts. Preserve publication/acceptance timing in a new historical panel contract. Until that exists, label SEC results current-snapshot analytics only.

### 6. P1 — Concurrent Bloomberg requests can consume each other's events

**Evidence:** [engine.py](../src/trade_research/engine.py), concurrent thread execution, lines 153–156 and 207–208; [providers/remote.py](../src/trade_research/providers/remote.py), lines 282–360. The lock protects session creation only. Each caller sends a request and consumes the shared session event stream without correlation-ID routing.

**Impact:** several selected price skills or concurrent users can race on the same session. Rows are then assigned the requested instrument by the adapter, so downstream identity validation cannot detect a response that belonged to another request. This is a code-path finding; live Bloomberg concurrency was not exercised.

**Fix:** serialize the entire bounded request/response cycle initially, or implement explicit correlation-ID dispatch if measured demand justifies it. Add interleaved synthetic response tests, an overall deadline, field-error checks, deterministic shutdown, and leap-day-safe date arithmetic (`today.replace(year=...)` currently fails on February 29). Verify the actual Desktop/Server/BPIPE authentication and entitlements separately; naming it BPIPE does not establish those capabilities.

### 7. P1 — Presentation data bypasses the intended licensed-data projection

**Evidence:** [skills/backtesting.py](../src/trade_research/skills/backtesting.py), lines 818–830 and 943, places source OHLC into `price_bars`; [reporting.py](../src/trade_research/reporting.py), line 129, preserves `presentation`; [application.py](../src/trade_research/application.py), `run_skill`/`research`, always saves the report. Other price-chart presentations also need review.

**Impact:** a Bloomberg-backed run can carry raw licensed prices into stored/exported reports even though evidence text is removed. Immediate-only execution prevents queue-input persistence, not report-output persistence. This conflicts with the repository's stated raw-data boundary; no actual disclosure was observed.

**Fix:** make raw-series retention/export an explicit source entitlement decision at the common report boundary. Default restricted sources to derived summaries and opaque authorized dataset references. Test JSON, Markdown, report storage, and all typed presentations with synthetic restricted data.

### 8. P2 — Documented retries do not retry HTTP 429/5xx

**Evidence:** [providers/remote.py](../src/trade_research/providers/remote.py), `_http_get`, lines 97–131. Retryable status codes raise `ProviderConfigurationError`, which is immediately re-raised. Only the caught transport exceptions reach backoff.

**Reproduction:** a mocked 429 produces one `httpx.get` call, not three.

**Fix:** separate transient status failures from permanent contract/configuration failures; honor bounded Retry-After/backoff and a total deadline. Read response bodies with a streaming byte cap—the current check happens after downloading the full body.

### 9. P2 — Parameter and skill metadata are not one enforced contract

**Evidence:** [skills/parameters.py](../src/trade_research/skills/parameters.py), early parameter models inherit plain `BaseModel`, unlike `_BacktestParameters` with closed fields. `TechnicalSkillParameters.model_validate({'windwo': 60})` silently returns `window=20`. [application.py](../src/trade_research/application.py), `describe_skill`, separately hardcodes scope and supported asset types; engine configuration does not enforce that whole catalogue.

**Impact:** mistakes can silently run a different study. Catalogue availability means a capability exists or can be supplied inline, not that a source covers this market. A default single `PRICES` provider cannot simultaneously route remote Yahoo equities and CCXT crypto.

**Fix:** one small immutable skill descriptor should own parameter schema, scope, supported instrument kinds, and required capabilities. Reject unknown fields consistently. Report configuration readiness separately from per-request data coverage. Add explicit source routing only for selected series, without silent provider fallback.

### 10. P2 — Crypto annualization differs across skills

**Evidence:** [skills/core.py](../src/trade_research/skills/core.py), `TechnicalSkill`, lines 289 and 485–493, always uses 252. [skills/price_series.py](../src/trade_research/skills/price_series.py), line 40, uses 365 for crypto. The catalogue advertises `technical` for crypto.

**Impact:** with identical daily-return windows, scaling by sqrt(252) rather than sqrt(365) understates a 365-day volatility convention by approximately 17%. Different existing lookbacks are another reason not to compare the two output numbers directly.

**Fix:** centralize frequency/calendar conventions, include them in report metadata, and test equal-window numerical parity.

### 11. P2 — CI documentation discovery is currently inconsistent

**Evidence:** [.github/workflows/ci.yml](../.github/workflows/ci.yml), “Check skill documentation.” It expects every runtime skill to have `skills/<runtime-name>/SKILL.md`.

**Reproduction:** missing paths are `fundamental`, `technical`, `filings`, `technical-basic`, `risk-analysis`, and `volatility-regime`. Some documents exist under different names; others are covered only in the README. Therefore passing pytest/ruff/mypy alone does not mean CI passes.

**Fix:** use the canonical descriptor's documentation path or align the directories. Add genuine missing method documentation rather than empty files.

### 12. P2 — Repeated fetching weakens reproducibility and factor scalability

**Evidence:** [engine.py](../src/trade_research/engine.py), shared provider registry passed to concurrent analysts; [providers/registry.py](../src/trade_research/providers/registry.py), `prices`, fetches on every call. Local CSV/Parquet readers bound the entire file before filtering by symbol ([local.py](../src/trade_research/providers/local.py), lines 28–92).

**Impact:** multiple skills can independently fetch the same asset and see different snapshots. A normal equity panel exceeds the 4,096-row local price-file limit even though each instrument is small. The nine-instrument request bound cannot support meaningful broad-universe research.

**Fix:** request-scoped, thread-safe snapshot reuse and typed bulk panel queries with separate limits. Do not remove existing request caps globally or add a distributed cache before measuring demand. A content hash supports identification, but reproducing a run also requires an accessible authorized snapshot and normalized method parameters.

## Simplification and scope decisions

| Component | Recommendation | Evidence / qualification |
|---|---|---|
| `ResearchApplication` → `ResearchEngine` → fixed skills/providers | Keep | Existing transport separation is useful; a rewrite would duplicate working contracts. |
| `ResearchCompiler` | Remove after external-use check | Exported and tested, but no application execution caller found; duplicates part of engine orchestration. |
| `RunStore` | Remove standalone store after external-use check | Public export/tests, while runtime uses `JobQueue`. Keep/refactor the safe persisted-request DTO that the queue actually uses. |
| `ObservationStore` | Remove unused persistence API or give it one explicit owner | No runtime callers found. JSON strings in a Parquet column are not a queryable factor panel. Do not reuse it as a panel store by name alone. |
| `Position` / `PortfolioProvider` / `LocalPortfolioProvider` | Candidate removal | No analyst found consuming actual positions; allocation uses instrument lists. Preserve allocation analytics, avoid adding account scope. |
| `DiscordNotifier` and webhook setting | Candidate removal if no external consumer | Implementation exists, but no application/worker notification hook found. Do not claim working automated notifications. |
| Old configuration aliases | Remove in a deliberate breaking cleanup | `TRADE_RESEARCH_PROVIDER`, duplicate `sec_cik_map`/`sec_cik_overrides`, and Markov's compatibility `threshold` add multiple ways to express the same input. |
| `_row_float` in `remote.py` | Delete | No call sites found. |
| `technical` vs `technical-basic` | Keep distinct outputs; consolidate numerical primitives | Indicator table versus confirmation score serve different questions. Do not merge by name similarity. |
| EMA/SMA implementations and hand-coded matrix solver | Consolidate incrementally | Backtesting, indicators, and classic signals have overlapping primitives with potentially different warm-up rules. Preserve intentional semantics through numerical fixtures; use NumPy/SciPy for new linear algebra. |
| `backtesting` vs `signal-evaluation` | Keep separate | Capital-path simulation versus event-outcome evidence. Neither replaces cross-sectional evaluation. |
| Worth-buy/Markov/classic strategies | Keep as existing optional consumers | Not proven obsolete. Do not expand them into a factor library or treat hand-weighted scores as established alpha. |
| China-specific markets, Tencent/Mootdx | Freeze expansion; remove only after caller check | Outside requested priority, but documented Vibe Research inputs may still use them. “Not my focus” is not evidence of dead code. |
| Hermes/crypto/showcase | Keep optional; no new scope now | Separate existing entry points/use cases, not necessary dependencies of factor research. |
| `ImpPlan.md`, `implementation_plan.md`, `TradeAgentReview.md` | Archive superseded plans; one current design index | Old project description mentions removed Stooq; plans describe work already implemented. They must not compete with current contracts. |

Avoid broad cleanup before a working factor slice exists. The request permits a simpler design without compatibility layers, but public exports and documented integrations still deserve a caller inventory before removal.

## Additional engineering follow-ups

- `run_skill` and multi-skill `analyze` take different execution paths and handle failures differently. Consolidate execution and preserve typed partial/failure reporting across transports.
- HTTP queued submission does not catch all `ValueError` rejections (including immediate-only work), unlike `/analyze`; test consistent 422 responses.
- `ReportStore.save` uses fixed temporary filenames per UUID and a completion marker that can already exist on overwrites. Concurrent writes/reuse of a request ID deserve atomicity/conflict tests. This is a static risk, not a reproduced corruption incident.
- Reporting deliberately replaces explanatory summaries with canned text. New beginner-facing explanations should use typed diagnostic/reason fields, otherwise useful model warnings will disappear during export.
- Deployment defaults permit mutable Python image tags; CI does not establish release digest approval. Compose syntax validation is not a release-policy or live deployment check. Configured local source paths also need a container test because sources mount under `/var/lib/trade-research-sources` while `data_root` defaults under `/var/lib/trade-research`.
- NumPy/pandas already exist in the lock through backtesting; code that directly depends on them should declare them directly. Optional Bloomberg/CCXT dependencies use broad ranges and need a tested, reproducible optional install path.
- Preserve existing good controls: bearer authentication, disabled HTTP access logs, bounded tools, fixed SQL tables, no broker/order path, typed provenance, lease fencing, and synthetic fixtures. A factor framework does not require relaxing these boundaries.

## Recommended order

1. Correct adjustment, instrument/session/currency, queue, SEC-period, and Bloomberg/report-boundary issues with focused failing tests.
2. Fix CI discovery and consolidate only the metadata and data-preparation code needed for factors.
3. Ship one single-stock factor regression through existing interfaces.
4. Add a dated equity panel and cross-sectional evaluator.
5. Add optional ridge/PCA and richer fundamental factors after walk-forward validation works.
