# Filings Analysis Skill

**Name:** `filings`
**Required capability:** `FILINGS`
**Implementation:** `src/trade_research/skills/core.py:FilingsSkill`

## Purpose

Analyzes SEC EDGAR filing history for a given symbol. Produces four metrics:
form-type counts and the age of the most recent annual (10-K) and quarterly (10-Q) reports.
Useful for assessing reporting recency, disclosure frequency, and material-event activity.

## Metrics computed

| Metric | Description | Algorithm |
|---|---|---|
| `recent_filing_count` | Total number of recent filings | Count of all Evidence items returned |
| `material_event_count` | Count of 8-K and 8-K/A filings | Count of evidence with form ∈ {`8-K`, `8-K/A`} |
| `annual_report_age_days` | Days since latest 10-K or 10-K/A | `now.date() − max filing_date` over {`10-K`, `10-K/A`} |
| `quarterly_report_age_days` | Days since latest 10-Q or 10-Q/A | `now.date() − max filing_date` over {`10-Q`, `10-Q/A`} |

## Algorithm

1. **Fetch evidence:** `ProviderRegistry.filings()` calls `SecFilingsProvider.filings()`,
   which hits the SEC EDGAR submissions endpoint and returns up to `MAX_FILING_ROWS`
   normalized `Evidence` objects.

2. **Parse:** `_parse_filing_evidence()` extracts `form` and `filing_date` from each
   Evidence's JSON content. Non-JSON, malformed, or evidence with missing fields is
   silently skipped.

3. **Classify:** Filings are bucketed into 10-K, 10-Q, and 8-K groups (each includes
   their `/A` amendment variants).

4. **Compute:** Counts are direct. Ages are computed as the difference in days between
   the current UTC date and the latest filing date in each category.

## Input data requirements

- SEC EDGAR CIK mapping must be configured per-symbol in `settings.sec_cik_map`
- A valid `User-Agent` string in `settings.sec_user_agent` per SEC requirements
- Provider returns `Evidence` objects with JSON content: `{"form": "<type>", "filing_date": "<ISO date>"}`

## Configuration

```json
{
  "sec_user_agent": "Your Org Name (contact@example.com)",
  "sec_cik_map": {
    "AAPL": "0000320193",
    "MSFT": "0000789019"
  }
}
```

Or via environment variables:
```sh
export TRADE_RESEARCH_SEC_USER_AGENT="Your Org Name (contact@example.com)"
# CIK map must come from JSON config: TRADE_RESEARCH_CONFIG=/path/to/config.json
```

## Output

Returns `AnalystResult` with:
- `status`: `complete` if all 4 metrics computed, `partial` otherwise
- `observations`: derived metrics with `source="derived"`, `input_provider_kind="sec"`

## Failure modes

| Condition | Behavior |
|---|---|
| No `sec_user_agent` or `sec_cik_map` configured | Provider is not registered; skill won't be selected |
| Symbol not in CIK map | `ProviderConfigurationError` via `SecFilingsProvider` |
| SEC endpoint returns malformed JSON | `ProviderContractError` |
| No 10-K filings found | `annual_report_age_days` is missing |
| No 10-Q filings found | `quarterly_report_age_days` is missing |
| No filings at all | Returns `partial` with empty observations |
