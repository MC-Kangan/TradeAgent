# TradeAgent MVP Implementation Plan

## Goal

Complete a reliable US-equity research MVP using the existing architecture.

Keep the scope narrow:

```text
Prices → existing Yahoo HTTP provider
Financial statements → SEC Company Facts
Filing history → SEC EDGAR submissions
Analysis → existing deterministic Python skills
Output → JSON and Markdown reports
```

Do not add `yfinance`, multi-agent debate, news analysis, or complex provider fallback yet.

---

## 1. Keep and Harden the Existing Yahoo Price Provider

The existing Yahoo HTTP provider already retrieves OHLCV price history, so it should remain the MVP price source.

### Required changes

Add only the missing reliability controls:

* Retry transient failures such as timeouts, HTTP 429 and HTTP 5xx.
* Use exponential backoff with a small retry limit, for example three attempts.
* Return a clear typed provider error after retries are exhausted.
* Add one opt-in live smoke test using a stable ticker such as `AAPL`.
* Document whether returned prices are adjusted or unadjusted.
* Reject malformed or incomplete OHLCV rows.

### Suggested behaviour

```text
Request Yahoo chart endpoint
        ↓
Validate HTTP status and response size
        ↓
Parse timestamp and OHLCV arrays
        ↓
Discard rows with no closing price
        ↓
Validate finite values and OHLC consistency
        ↓
Return bounded PricePoint objects
```

### Do not add yet

* A second Yahoo implementation
* Complex provider fallback
* Automatic switching between Yahoo and Stooq
* Intraday pricing
* Corporate-action reconstruction

---

## 2. Add an SEC Company Facts Fundamental Provider

This is the most important missing MVP feature.

The current fundamental skill works only when the user supplies local CSV, Parquet or SQL data. Add a remote provider that converts SEC Company Facts data into the existing `Observation` model.

### New provider

Suggested class:

```python
SecCompanyFactsProvider
```

Suggested capability:

```python
CapabilityName.FUNDAMENTALS
```

The provider should:

1. Resolve the ticker to a CIK.
2. Request:

```text
https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
```

3. Extract approved US-GAAP concepts.
4. Select compatible current and prior periods.
5. Convert them into normalised `Observation` objects.
6. Preserve source and period provenance.

### Initial concept mapping

Start with a small allowlist:

| TradeAgent metric     | SEC concepts to consider                                                  |
| --------------------- | ------------------------------------------------------------------------- |
| `revenue`             | `RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`         |
| `net_income`          | `NetIncomeLoss`                                                           |
| `operating_income`    | `OperatingIncomeLoss`                                                     |
| `shareholders_equity` | `StockholdersEquity`                                                      |
| `total_debt`          | selected current and non-current debt concepts                            |
| `free_cash_flow`      | derive from operating cash flow minus capital expenditure                 |
| `ebitda`              | leave unavailable initially unless a consistent derivation is implemented |

Do not attempt to support every possible XBRL concept in the first version.

### Period selection

Prefer:

```text
TTM where available
otherwise latest annual period
```

For growth calculations, select:

```text
current annual period
prior comparable annual period
```

Do not compare:

* Annual against quarterly
* Different currencies
* Different consolidated scopes
* Duplicate amended facts without deterministic selection

### Provenance fields

Each returned observation should include:

```text
provider_kind
CIK
SEC concept
accession or filing reference
period_start
period_end
period_type
period_role
currency
snapshot_ref
reference
```

### Failure behaviour

Return partial fundamental results when some metrics are unavailable.

Examples:

```text
Revenue available
Net income available
EBITDA unavailable
Enterprise value unavailable
```

The entire fundamental skill should not fail because one metric is missing.

---

## 3. Add Automatic Ticker-to-CIK Resolution

The current filings provider requires a manually configured ticker-to-CIK map.

Replace this requirement with an SEC mapping loader.

### Data source

Use the SEC-published company ticker mapping.

The resolver should:

1. Download the mapping using the configured SEC user agent.
2. Validate and normalise symbols.
3. Cache it locally.
4. Store a retrieval timestamp and content hash.
5. Refresh only when the cache exceeds a configured age.
6. Allow manual overrides in settings.

### Suggested interface

```python
class CikResolver:
    def resolve(self, symbol: str) -> str:
        ...
```

Both of these providers should use it:

```text
SecCompanyFactsProvider
SecFilingsProvider
```

### Configuration

Keep only:

```text
TRADE_RESEARCH_SEC_USER_AGENT
```

Allow an optional override map:

```json
{
  "sec_cik_overrides": {
    "BRK.B": "0001067983"
  }
}
```

This handles unusual symbols without forcing users to maintain the full mapping.

---

## 4. Separate Statement Data from Valuation Data

SEC Company Facts provides accounting data but generally does not provide the current market values required for:

* P/E
* EV/EBITDA
* FCF yield

The MVP should treat these as a separate data category.

### Simple option

Extend the existing Yahoo provider to retrieve:

```text
market_cap
enterprise_value
valuation_as_of
currency
```

only if the current Yahoo endpoint already exposes those values reliably.

### Safer MVP alternative

Allow the fundamental skill to return partial results without valuation ratios.

The first MVP can still calculate:

* Revenue growth
* Earnings growth
* Operating margin
* Net margin
* ROE
* Free cash flow
* FCF margin
* Leverage

Then add valuation fields in a later iteration.

This is simpler than introducing a second remote provider immediately.

### Recommendation

For the first implementation:

```text
SEC Company Facts → accounting metrics
Yahoo HTTP → prices only
Valuation ratios → partial/unavailable
```

Add market-cap and EV support only after the statement provider works reliably.

---

## 5. Keep Filing Analysis Simple

The existing filing skill is adequate for the MVP once CIK resolution is automatic.

Keep the output limited to:

* Total recent filing count
* Recent 8-K count
* Days since latest 10-K
* Days since latest 10-Q

### Minor improvements

Include additional filing metadata in provider evidence:

```text
accession_number
primary_document
report_date
filing_date
form
is_amendment
```

Do not perform full-text SEC filing analysis yet.

The skill should remain deterministic and metadata-based.

---

## 6. Fix Capability Diagnostics

The doctor command should report readiness by skill rather than demanding all providers.

### Suggested output

```text
Technical analysis: ready
Fundamental analysis: ready
Filings analysis: ready
Combined analysis: ready
```

Or, for partial configuration:

```text
Technical analysis: ready
Fundamental analysis: unavailable — SEC provider not configured
Filings analysis: ready
Combined analysis: unavailable
```

### Implementation

Calculate readiness using registered skill requirements:

```python
technical_ready = providers.has(PRICES)
fundamental_ready = providers.has(FUNDAMENTALS)
filings_ready = providers.has(FILINGS)
```

Do not use:

```python
prices_available and fundamentals_available
```

as the definition of a valid installation.

---

## 7. Add Minimal CI

Create one GitHub Actions workflow.

### Required commands

```bash
pytest
ruff check .
mypy
```

Optionally add:

```bash
python -m build
```

Do not build a complicated release pipeline yet.

### Required CI checks

* Unit tests
* Type checks
* Linting
* Package build
* Verification that every registered skill has a corresponding `SKILL.md`

### Example documentation test

```python
def test_registered_skills_have_docs() -> None:
    for name in default_skill_registry.names:
        expected = SKILL_DOC_PATHS[name]
        assert expected.is_file()
```

---

## 8. Fix Instrument Identity

All local and cached data should identify an instrument using:

```text
market + symbol
```

Recommended representation:

```text
NASDAQ:AAPL
NYSE:IBM
LSE:VOD
```

### Schema update

Add `market` to:

* Price CSV schema
* Fundamental CSV schema
* Parquet schema
* SQLite tables
* Cached provider data

Provider queries must filter by both values.

For the initial US-only MVP, market can default only when the input is unambiguous and explicitly validated. It should not be silently guessed for long-term storage.

---

## 9. Make Report Writes Atomic

Reports should not be written directly to their final paths.

### Suggested process

```text
Write JSON to temporary file
Write Markdown to temporary file
Flush both files
Atomically rename both
Write completion marker last
```

Readers should only return reports with a valid completion marker.

This prevents half-written or mismatched reports after a crash.

---

## 10. Recommended Build Order

### Task 1 — SEC CIK resolver

Implement and test automatic ticker-to-CIK resolution.

### Task 2 — SEC Company Facts provider

Map a small allowlist of accounting concepts into `Observation` records.

### Task 3 — Fundamental skill integration

Register the remote provider and verify partial metric handling.

### Task 4 — Yahoo reliability

Add retry, backoff, live smoke test and explicit adjustment documentation.

### Task 5 — Doctor command

Report technical, fundamental and filings readiness independently.

### Task 6 — Minimal CI

Run tests, Ruff and mypy on every pull request.

### Task 7 — Instrument identity

Add market-aware matching to local schemas and caches.

### Task 8 — Atomic reporting

Protect JSON and Markdown output from partial writes.

---

## MVP Acceptance Test

The MVP is successful when a user can run:

```bash
trade-research run-skill technical AAPL --market NASDAQ
trade-research run-skill fundamental AAPL --market NASDAQ
trade-research run-skill filings AAPL --market NASDAQ
```

without manually preparing a dataset or entering a CIK.

Expected results:

```text
Technical:
- Yahoo OHLCV data
- Deterministic indicators

Fundamental:
- SEC financial statement metrics
- Partial output where unsupported metrics are missing

Filings:
- SEC filing activity and recency

Report:
- Valid JSON
- Valid Markdown
- Provider provenance
- Clear missing-data limitations
```

---

## Defer Until After MVP

Do not implement yet:

* `yfinance`
* News and sentiment
* Full filing-document interpretation
* Multi-agent debates
* Dynamic skill generation
* Global fundamentals
* Multiple automatic fallback vendors
* Broker connections
* Trade execution
* Portfolio optimisation

The simplest effective MVP is:

```text
Existing Yahoo HTTP provider
+ SEC CIK resolution
+ SEC Company Facts
+ SEC filing metadata
+ deterministic skills
+ basic CI
```
