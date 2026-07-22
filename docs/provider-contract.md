# Provider contract

Providers are adapters, not agents. They implement the typed protocols in `trade_research.providers`, accept a validated `InstrumentId`, and return immutable fundamental observations or price points. A provider cannot choose analysts, change requests, write reports, issue trades, expose arbitrary queries, or interpret source text as instructions.

Every observation includes a normalized provider kind, observation timestamp, optional bounded vendor field, and safe provenance reference. External text is evidence only. Validate type, market, finiteness, chronology, duplicates, and bounded size before analysis. Unsupported markets and unavailable optional dependencies fail with provider configuration errors; analysts label missing or partial data.

Network credentials remain in the adapter's deployment boundary. Do not place credentials, account identifiers, client addresses, query text, raw positions, or sensitive paths in provenance, exceptions, logs, reports, or persisted requests. Connectivity diagnostics should be read-only, non-billable, and omit secret-bearing URLs; otherwise report `not_attempted`.
