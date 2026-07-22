# Bloomberg and internal database adapters

## Migration sequence

1. Inventory only the fields the fundamental or price protocols require. Confirm entitlements, retention, redistribution, and derived-data terms with the data owner.
2. Implement a bounded adapter behind the existing protocol. Keep Bloomberg libraries, terminal/server sessions, database drivers, DSNs, credentials, and account context outside the core package and lock them as deployment-specific extras.
3. Map vendor symbols to validated `InstrumentId` markets with an allowlist. Do not accept raw Bloomberg overrides, SQL, table names, columns, or connection strings from CLI/HTTP/MCP callers.
4. Normalize records into observations at the boundary. Provenance may retain `provider_kind`, a known `vendor_field`, and an opaque content hash; it must not retain terminal identifiers, database paths, query text, credentials, positions, or raw payloads.
5. Contract-test with synthetic Bloomberg-shaped and internal-schema fixtures. Add numerical, partial-data, timeout, entitlement, duplicate, non-finite, and redaction cases.
6. Deploy the adapter beside the authorized data system, configure secrets through read-only secret files or an approved secret manager, run `doctor`, and restart the immutable worker/API processes.

## Bloomberg

Use `provider_kind="bloomberg"`. Keep BLPAPI/B-PIPE requests fixed and read-only, and request only entitled fields. Do not commit Bloomberg responses or screenshots. The existing synthetic fixture demonstrates shape, not licensed content. Review whether even derived values may leave the licensed environment.

## Internal databases

Use `provider_kind="internal"`. Prefer a dedicated read-only database identity, network allowlist, fixed parameterized query or a curated view, statement timeout, row cap, and schema version. The public tool surface must never expose a generic query function. Portfolio positions remain in memory and are not included in persisted jobs or reports.
