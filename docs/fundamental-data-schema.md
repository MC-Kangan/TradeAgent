# Fundamental data schema

This document describes the closed, typed schema expected by the fundamental data
providers (`LocalCsvParquetFundamentalProvider` and `ReadOnlySqlFundamentalProvider`).
Every row must produce a valid `Observation` with auditable provenance.

## Required fields

| Field | Type | Description |
| --- | --- | --- |
| `symbol` | string | Instrument ticker, matched case-insensitively |
| `metric` | string | A known v1 `MetricKind` value (see below) |
| `value` | number | Finite numeric observation |
| `observed_at` | string | ISO-8601 timestamp with timezone (e.g. `2026-01-15T00:00:00+00:00`) |

## Optional fields

| Field | Type | Present for | Description |
| --- | --- | --- | --- |
| `period_role` | string | Statement metrics | `current` or `prior` |
| `period_end` | string | Statement metrics | ISO-8601 date (e.g. `2025-12-31`) |
| `period_type` | string | Statement metrics | `annual`, `quarterly`, or `ttm` |
| `period_ref` | string | Statement metrics | Opaque period identifier |
| `prior_period_ref` | string | Current statement metrics | Opaque prior period identifier |
| `snapshot_ref` | string | All | Opaque snapshot identifier (falls back to file hash) |
| `currency` | string | All | ISO 4217 three-letter code (e.g. `USD`) |
| `valuation_as_of` | string | Valuation metrics | ISO-8601 timestamp with timezone |
| `vendor_field` | string | All | Source vendor field label |

## Statement metrics vs. valuation metrics

**Statement metrics** are financial-statement line items that carry period metadata:

```
revenue, net_income, earnings, operating_income, shareholders_equity,
total_equity, free_cash_flow, total_debt, ebitda, gross_profit
```

Statement rows require: `period_role`, `period_end`, `period_type`, `period_ref`,
and `currency`.

**Valuation metrics** are point-in-time market snapshots:

```
market_cap, enterprise_value
```

Valuation rows require: `valuation_as_of` and `currency`.

## CSV example

```csv
symbol,metric,value,observed_at,period_role,period_end,period_type,period_ref,prior_period_ref,currency,snapshot_ref,vendor_field
AAPL,revenue,120000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001,REVENUE
AAPL,revenue,100000000000,2026-01-15T00:00:00+00:00,prior,2024-09-30,annual,FY2024,,USD,snap-001,REVENUE
AAPL,net_income,12000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001,NET_INCOME
AAPL,market_cap,3000000000000,2026-01-15T00:00:00+00:00,,,,,,USD,snap-001,MARKET_CAP
AAPL,ebitda,18000000000,2026-01-15T00:00:00+00:00,current,2025-09-30,annual,FY2025,FY2024,USD,snap-001,EBITDA
```

## SQLite schema

The fixed table name is `fundamentals`:

```sql
CREATE TABLE fundamentals (
    symbol           TEXT NOT NULL,
    metric           TEXT NOT NULL,
    value            REAL NOT NULL,
    observed_at      TEXT NOT NULL,
    period_role      TEXT,
    period_end       TEXT,
    period_type      TEXT,
    period_ref       TEXT,
    prior_period_ref TEXT,
    snapshot_ref     TEXT,
    currency         TEXT,
    valuation_as_of  TEXT,
    vendor_field     TEXT
);
```

The provider reads from the table using the columns above. All columns except the first
four are optional in the schema, but statement and valuation metrics are validated against
their respective metadata requirements at query time.

## Bounds

| Limit | Value |
| --- | --- |
| Maximum rows returned | `MAX_FUNDAMENTAL_ROWS` (256) |
| Maximum file size | `MAX_LOCAL_BYTES` (64 MiB) |
| Parquet metadata rows | Must be ≤ `MAX_FUNDAMENTAL_ROWS` |

## Validation

Every observation row is validated against the closed provenance schema:

- `provider_kind` must match the source (`local_csv`, `local_parquet`, `local_sql`)
- `snapshot_ref` and `reference` must be SHA-256 content hashes
- Timestamps must include timezone
- Numeric values must be finite
- Statement metadata: `period_role` ∈ {`current`, `prior`}, `period_type` ∈ {`annual`, `quarterly`, `ttm`}
- Valuation metadata: `valuation_as_of` must be a valid ISO-8601 timestamp
- `period_end` must not be in the future relative to `observed_at`
- `currency` must be three uppercase letters

Rows that fail validation produce `ProviderContractError` — the provider returns no partial
data.
