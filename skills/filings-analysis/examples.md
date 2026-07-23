# Filings Analysis Examples

## Complete data scenario

SEC returns 7 filings for a symbol:

```json
[
  {"form": "10-K", "filing_date": "2025-03-15", "reference": "sha256:..."},
  {"form": "10-Q", "filing_date": "2025-11-01", "reference": "sha256:..."},
  {"form": "10-Q", "filing_date": "2025-08-01", "reference": "sha256:..."},
  {"form": "10-Q", "filing_date": "2025-05-01", "reference": "sha256:..."},
  {"form": "8-K", "filing_date": "2025-06-15", "reference": "sha256:..."},
  {"form": "8-K", "filing_date": "2025-09-20", "reference": "sha256:..."},
  {"form": "8-K/A", "filing_date": "2025-09-22", "reference": "sha256:..."}
]
```

Expected output (assuming today is 2026-07-23):

| Metric | Value | Notes |
|---|---|---|
| recent_filing_count | 7 | All filings counted |
| material_event_count | 3 | 8-K × 2, 8-K/A × 1 |
| annual_report_age_days | 495 | Days since 2025-03-15 |
| quarterly_report_age_days | 264 | Days since latest 10-Q (2025-11-01) |

Status: `complete`, summary: `"complete data: all required inputs available"`

## Only 8-K filings

When a company has only material-event filings with no annual/quarterly reports:

```
Form: 8-K, Date: 2025-06-15
```

Expected output:

| Metric | Value |
|---|---|
| recent_filing_count | 1 |
| material_event_count | 1 |

`annual_report_age_days` and `quarterly_report_age_days` are missing.

Status: `partial`

## No filings at all

When the SEC returns zero recent filings:

Expected output:
- Observations: empty tuple
- Status: `partial`
- Summary: `"partial data: missing no filing data available"`

## CLI invocation

```sh
trade-research run-skill filings AAPL
```
