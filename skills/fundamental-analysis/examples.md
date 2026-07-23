# Fundamental Analysis Examples

## Complete data scenario

Input observations (from a CSV or Bloomberg adapter):

```csv
symbol,metric,value,observed_at,period_role,period_end,period_type,period_ref,prior_period_ref,currency,snapshot_ref
AAPL,revenue,120000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001
AAPL,revenue,100000000000,2026-01-15T00:00:00+00:00,prior,2024-09-30,annual,FY2024,,USD,snap-001
AAPL,net_income,12000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001
AAPL,net_income,10000000000,2026-01-15T00:00:00+00:00,prior,2024-09-30,annual,FY2024,,USD,snap-001
AAPL,operating_income,20000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001
AAPL,shareholders_equity,60000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001
AAPL,free_cash_flow,18000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001
AAPL,total_debt,30000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001
AAPL,market_cap,3000000000000,2026-01-15T00:00:00+00:00,,,,,,USD,snap-001
AAPL,enterprise_value,3200000000000,2026-01-15T00:00:00+00:00,,,,,,USD,snap-001
AAPL,ebitda,20000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001
```

Expected output:

| Metric | Value |
|---|---|
| revenue_growth | 0.2 |
| earnings_growth | 0.2 |
| operating_margin | 0.1667 |
| net_margin | 0.1 |
| return_on_equity | 0.2 |
| free_cash_flow | 18000000000.0 |
| free_cash_flow_margin | 0.15 |
| leverage | 0.5 |
| price_to_earnings | 250.0 |
| enterprise_value_to_ebitda | 160.0 |
| free_cash_flow_yield | 0.006 |

Status: `complete`, summary: `"complete data: all required inputs available"`

## Partial data scenario

If only `revenue` and `net_income` are provided:

```csv
symbol,metric,value,observed_at,period_role,period_end,period_type,period_ref,prior_period_ref,currency,snapshot_ref
AAPL,revenue,120000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001
AAPL,revenue,100000000000,2026-01-15T00:00:00+00:00,prior,2024-09-30,annual,FY2024,,USD,snap-001
AAPL,net_income,12000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001
AAPL,net_income,10000000000,2026-01-15T00:00:00+00:00,prior,2024-09-30,annual,FY2024,,USD,snap-001
```

Expected output:

| Metric | Value |
|---|---|
| revenue_growth | 0.2 |
| earnings_growth | 0.2 |

All 9 other metrics are missing. Status: `partial`, summary lists each missing metric.

## Incompatible periods

When current and prior observations have mismatched metadata:

```csv
symbol,metric,value,observed_at,period_role,period_end,period_type,period_ref,prior_period_ref,currency,snapshot_ref
AAPL,revenue,120000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001
AAPL,revenue,100000000000,2026-01-15T00:00:00+00:00,prior,2024-09-30,quarterly,Q4-2024,,EUR,snap-002
```

Result: `revenue_growth` is skipped because period types differ (`annual` vs `quarterly`),
currencies differ (`USD` vs `EUR`), and snapshots differ (`snap-001` vs `snap-002`).
Summary: `"partial data: missing revenue_growth incompatible input metadata"`

## CLI invocation

```sh
trade-research run-skill fundamental AAPL
trade-research run-skill fundamental AAPL --market NASDAQ
```
