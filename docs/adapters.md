# Bloomberg and internal database adapters

## Migration sequence

1. Inventory only the fields the fundamental or price protocols require. Confirm entitlements, retention, redistribution, and derived-data terms with the data owner.
2. Implement a bounded adapter behind the existing protocol. Keep Bloomberg libraries, terminal/server sessions, database drivers, DSNs, credentials, and account context outside the core package and lock them as deployment-specific extras.
3. Map vendor symbols to validated `InstrumentId` markets with an allowlist. Do not accept raw Bloomberg overrides, SQL, table names, columns, or connection strings from CLI/HTTP/MCP callers.
4. Normalize records into observations at the boundary. Provenance may retain `provider_kind`, a known `vendor_field`, and an opaque content hash; it must not retain terminal identifiers, database paths, query text, credentials, positions, or raw payloads.
5. Contract-test with synthetic Bloomberg-shaped and internal-schema fixtures. Add numerical, partial-data, timeout, entitlement, duplicate, non-finite, and redaction cases.
6. Deploy the adapter beside the authorized data system, configure secrets through read-only secret files or an approved secret manager, run `doctor`, and restart the immutable worker/API processes.

Production composition is configured only through the closed `TRADE_RESEARCH_PRICE_PROVIDER`,
`TRADE_RESEARCH_PRICE_PATH`, `TRADE_RESEARCH_FUNDAMENTAL_PROVIDER`,
`TRADE_RESEARCH_FUNDAMENTAL_PATH`, and `TRADE_RESEARCH_CCXT_EXCHANGE` settings (or the
equivalent allowlisted JSON config keys). Local normalized CSV/Parquet files and fixed read-only
SQLite `prices`/`fundamentals` tables are supported. A selected built-in analyst is rejected before
submission when its required capability is absent.

Under Docker Compose, keep these files in the host directory selected by
`TRADE_RESEARCH_SOURCE_DIR`. The API and worker see the same directory read-only at
`/var/lib/trade-research-sources`, so JSON configuration must use stable in-container paths such as
`/var/lib/trade-research-sources/prices.csv`; never put licensed inputs in the writable report and
queue volume.

The normalized price schema requires `symbol`, timezone-aware `observed_at`, and finite `close`;
`open`, `high`, `low`, and `volume` are optional. The normalized fundamental schema requires
`symbol`, a known `metric`, finite numeric `value`, and timezone-aware `observed_at`. Complete
fundamental factors additionally use the closed `period_role`, `period_end`, `period_type`,
`period_ref`, `prior_period_ref`, `snapshot_ref`, `currency`, `valuation_as_of`, and
`vendor_field` columns. SQLite uses only fixed `prices` and `fundamentals` tables and never accepts
table names, column names, or query text from a public interface.

## Bloomberg

Use `provider_kind="bloomberg"`. Keep BLPAPI/B-PIPE requests fixed and read-only, and request only entitled fields. Do not commit Bloomberg responses or screenshots. The existing synthetic fixture demonstrates shape, not licensed content. Review whether even derived values may leave the licensed environment.

## Internal databases

Use `provider_kind="internal"`. Prefer a dedicated read-only database identity, network allowlist, fixed parameterized query or a curated view, statement timeout, row cap, and schema version. The public tool surface must never expose a generic query function. Portfolio positions remain in memory and are not included in persisted jobs or reports.
