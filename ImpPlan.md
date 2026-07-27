# Bloomberg BPIPE Price Provider — Implementation Plan

## Context

The project has a pluggable provider architecture. `YahooPriceProvider` and `CcxtPriceProvider` already live in `src/trade_research/providers/remote.py`. Bloomberg should be added there — same file, same pattern — rather than as a separate file. The `resolve_provider_symbol` function in that file already handles per-provider symbol mapping and should be extended to cover Bloomberg tickers.

`ProviderKind.BLOOMBERG`, `VendorField.OHLCV`, and `VendorField.PX_LAST` are already registered in `domain/provenance.py`. `_canonical_reference` and `_optional_float` helpers already exist in `remote.py` and will be reused directly. No new files needed except the test file.

---

## What Changes

### 1. `src/trade_research/providers/remote.py` — extend existing file

**A. Add Bloomberg suffix map** (alongside the existing `_YAHOO_SUFFIXES`):
```python
_BLOOMBERG_SUFFIXES: dict[str, str] = {
    "US": " US Equity",   "NASDAQ": " US Equity",  "NYSE": " US Equity",
    "AMEX": " US Equity", "OTC": " US Equity",      "ETF": " US Equity",
    "LSE": " LN Equity",  "UK": " LN Equity",       "AIM": " LN Equity",
    "EURONEXT": " NA Equity", "XETRA": " GY Equity", "BME": " SM Equity",
    "BORSA_ITALIANA": " IM Equity", "SIX": " SW Equity", "EU": " EB Equity",
}
```
`CRYPTO` is deliberately absent; existing `resolve_provider_symbol` raises `ProviderConfigurationError` for unknown markets.

**B. Extend `resolve_provider_symbol`** to handle `"bloomberg"`:
Currently the `suffixes` dict on line 58 only maps `"yahoo"`. Extend it:
```python
suffixes = {"yahoo": _YAHOO_SUFFIXES, "bloomberg": _BLOOMBERG_SUFFIXES}.get(normalized_provider)
```
Bloomberg tickers need the *suffix appended with a space*, not lowercased (Yahoo lowercases for some markets; Bloomberg does not). The final `return` line conditionally lowercases only for Yahoo — Bloomberg gets `symbol` as-is. Adjust:
```python
if normalized_provider == "yahoo":
    return symbol.lower() if normalized_provider != "yahoo" else symbol
```
Actually the existing code does `return symbol if normalized_provider == "yahoo" else symbol.lower()` — Bloomberg should return `symbol` without lowercasing. Change to:
```python
return symbol if normalized_provider in {"yahoo", "bloomberg"} else symbol.lower()
```

**C. Add `BloombergPriceProvider` class** (after `CcxtPriceProvider`):

```python
class BloombergPriceProvider:
    """BPIPE historical OHLCV adapter with a lazy-init persistent session."""

    def __init__(self, host: str = "localhost", port: int = 8194) -> None:
        self._host = host
        self._port = port
        self._session: object | None = None
        self._service: object | None = None
        self._lock = threading.Lock()

    def _ensure_session(self) -> tuple[object, object]:
        with self._lock:
            if self._session is not None and self._service is not None:
                return self._session, self._service
            try:
                import blpapi  # type: ignore[import-not-found]
            except ImportError as error:
                raise OptionalProviderDependencyError(
                    "Bloomberg provider requires the optional 'blpapi' dependency. "
                    "Install it with: pip install blpapi --index-url "
                    "https://jfrog.corp.jefco.com/artifactory/api/pypi/"
                    "Python-remote-bloomberg/simple/"
                ) from error
            options = blpapi.SessionOptions()
            options.setServerHost(self._host)
            options.setServerPort(self._port)
            session = blpapi.Session(options)
            if not session.start():
                raise ProviderConfigurationError(
                    f"Bloomberg BPIPE session failed to start — "
                    f"ensure Bloomberg Terminal is running on {self._host}:{self._port}"
                )
            if not session.openService("//blp/refdata"):
                session.stop()
                raise ProviderConfigurationError(
                    "Bloomberg BPIPE could not open //blp/refdata service"
                )
            self._session = session
            self._service = session.getService("//blp/refdata")
            return self._session, self._service

    def __del__(self) -> None:
        if self._session is not None:
            try:
                self._session.stop()  # type: ignore[union-attr]
            except Exception:
                pass

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        import blpapi  # type: ignore[import-not-found]
        ticker = resolve_provider_symbol("bloomberg", instrument)
        session, service = self._ensure_session()

        today = datetime.now(UTC).date()
        start = today.replace(year=today.year - 1)
        request = service.createRequest("HistoricalDataRequest")  # type: ignore[union-attr]
        for field in ("PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST", "PX_VOLUME"):
            request.append("fields", field)
        request.append("securities", ticker)
        request.set("startDate", start.strftime("%Y%m%d"))
        request.set("endDate", today.strftime("%Y%m%d"))
        request.set("periodicitySelection", "DAILY")
        session.sendRequest(request)  # type: ignore[union-attr]

        raw_rows: list[dict[str, object]] = []
        while True:
            event = session.nextEvent(timeout=30000)  # type: ignore[union-attr]
            event_type = event.eventType()
            if event_type in (blpapi.Event.RESPONSE, blpapi.Event.PARTIAL_RESPONSE):
                for msg in event:
                    _bloomberg_process_message(msg, ticker, raw_rows)
            if event_type == blpapi.Event.RESPONSE:
                break
            if event_type == blpapi.Event.TIMEOUT:
                raise ProviderConfigurationError(
                    "Bloomberg BPIPE request timed out waiting for data"
                )

        if len(raw_rows) > MAX_PRICE_POINTS:
            raise ProviderContractError("Bloomberg exceeded the point limit")

        snapshot = _canonical_reference(raw_rows)
        return tuple(
            PricePoint(
                instrument=instrument,
                observed_at=datetime.strptime(str(row["date"]), "%Y-%m-%d").replace(tzinfo=UTC),
                close=float(row["close"]),  # type: ignore[arg-type]
                source="bloomberg",
                provenance={
                    "provider_kind": "bloomberg",
                    "vendor_field": "OHLCV",
                    "snapshot_ref": snapshot,
                    "reference": _canonical_reference(row),
                },
                open=_optional_float(row.get("open")),
                high=_optional_float(row.get("high")),
                low=_optional_float(row.get("low")),
                volume=_optional_float(row.get("volume")),
            )
            for row in raw_rows
            if row.get("close") is not None
        )
```

**D. Add `_bloomberg_process_message` private function** (after `BloombergPriceProvider`):
Extracts `fieldData` rows from a blpapi message and appends dicts to `raw_rows`. Raises `ProviderConfigurationError` on `responseError` or `securityError` elements in the message.

**E. Add `import threading` at the top** (alongside existing imports).

The existing `_canonical_reference` and `_optional_float` helpers are reused without any changes.

---

### 2. `src/trade_research/settings.py`

**Line 14** — add `"bloomberg"` to the `PriceProviderName` Literal:
```python
PriceProviderName = Literal["local_csv", "local_parquet", "local_sql", "yahoo", "ccxt", "bloomberg"]
```

**After line 28** (`ccxt_exchange` field) — two new optional fields with sensible defaults:
```python
bloomberg_host: str = "localhost"
bloomberg_port: int = 8194
```

**In `validate_paths`** — after the existing ccxt guard (lines 58–61), guard against setting bloomberg params for non-bloomberg providers:
```python
if self.price_provider != "bloomberg" and self.bloomberg_host != "localhost":
    raise ValueError("bloomberg_host is accepted only for the bloomberg provider")
if self.price_provider != "bloomberg" and self.bloomberg_port != 8194:
    raise ValueError("bloomberg_port is accepted only for the bloomberg provider")
```

**In `from_environment` `names` dict** — add two new env var mappings:
```python
"TRADE_RESEARCH_BLOOMBERG_HOST": "bloomberg_host",
"TRADE_RESEARCH_BLOOMBERG_PORT": "bloomberg_port",
```

---

### 3. `src/trade_research/engine.py`

In `_compose_providers`, add after the `ccxt` branch (after line 155):
```python
elif settings.price_provider == "bloomberg":
    from trade_research.providers.remote import BloombergPriceProvider
    providers["prices"] = BloombergPriceProvider(
        host=settings.bloomberg_host,
        port=settings.bloomberg_port,
    )
```

No change to the top-level imports. The deferred local import ensures the module loads even when blpapi is absent.

---

### 4. `src/trade_research/providers/__init__.py`

Add import alongside `CcxtPriceProvider` and `YahooPriceProvider`:
```python
from trade_research.providers.remote import (
    BloombergPriceProvider,   # add this
    CcxtPriceProvider,
    YahooPriceProvider,
    resolve_provider_symbol,
)
```
Add `"BloombergPriceProvider"` to `__all__`.

Safe to import at module level because `BloombergPriceProvider.__init__` does NOT import blpapi — the deferred import is inside `_ensure_session`.

---

### 5. `pyproject.toml`

Add an optional dependency group (after `crypto`):
```toml
[project.optional-dependencies]
bloomberg = [
  "blpapi>=3.24",
]
```

Add a mypy override since blpapi has no stubs:
```toml
[[tool.mypy.overrides]]
module = ["blpapi", "blpapi.*"]
ignore_missing_imports = true
```

---

### 6. `tests/test_bloomberg_provider.py` — NEW TEST FILE

All tests mock blpapi via `patch.dict(sys.modules, {"blpapi": mock_mod})`. No real Bloomberg data anywhere — AGENTS.md prohibits Bloomberg data in fixtures or commits. All values are clearly fictional (e.g. `101.0`, `99.0`).

**Test categories:**

*Symbol resolution (pure — no blpapi):*
- `test_resolve_ticker_us_equity` — `"AAPL"` + `"US"` → `"AAPL US Equity"`
- `test_resolve_ticker_lse` — `"VOD"` + `"LSE"` → `"VOD LN Equity"`
- `test_resolve_ticker_all_markets` (parametrized — all 15 markets, checks suffix)
- `test_resolve_ticker_crypto_raises` → `ProviderConfigurationError`

*Infrastructure / session:*
- `test_raises_when_blpapi_not_installed` → `OptionalProviderDependencyError`
- `test_raises_when_session_fails_to_start` → `ProviderConfigurationError`
- `test_raises_when_service_open_fails` → `ProviderConfigurationError`
- `test_session_is_reused_across_calls` → `session.start.call_count == 1`

*Happy path:*
- `test_price_history_ohlcv_fields` — all 5 numeric fields on returned `PricePoint`
- `test_price_history_provenance_keys` — `provider_kind`, `vendor_field`, `reference`, `snapshot_ref` format
- `test_snapshot_ref_is_deterministic` — same input → same hash on two independent calls
- `test_snapshot_ref_differs_when_data_differs`
- `test_rows_with_none_close_filtered`

*Error cases:*
- `test_raises_on_response_error`
- `test_raises_on_security_error`
- `test_raises_on_timeout`
- `test_raises_when_point_limit_exceeded`

*Settings integration:*
- `test_settings_accept_bloomberg_provider`
- `test_settings_accept_custom_host_and_port`
- `test_settings_reject_bloomberg_host_for_non_bloomberg_provider`

---

## Implementation Order (TDD)

1. Write `tests/test_bloomberg_provider.py` (all fail initially)
2. Add `_BLOOMBERG_SUFFIXES` + extend `resolve_provider_symbol` in `remote.py` → symbol tests pass
3. Add `BloombergPriceProvider` + `_bloomberg_process_message` to `remote.py` → all provider tests pass
4. Update `settings.py` → settings tests pass
5. Update `providers/__init__.py` and `engine.py`
6. Update `pyproject.toml`
7. Run `pytest`, `ruff check .`, `mypy`, `python -m build`

---

## Environment Configuration

### Environment variables (PowerShell syntax)

| Variable | Purpose | Default |
|---|---|---|
| `TRADE_RESEARCH_PRICE_PROVIDER` | Select the Bloomberg provider | _(none)_ |
| `TRADE_RESEARCH_BLOOMBERG_HOST` | BPIPE hostname | `localhost` |
| `TRADE_RESEARCH_BLOOMBERG_PORT` | BPIPE port | `8194` |

Minimal activation (Terminal running on localhost:8194):
```powershell
$env:TRADE_RESEARCH_PRICE_PROVIDER = "bloomberg"
```

Custom host/port (e.g. remote BPIPE server):
```powershell
$env:TRADE_RESEARCH_PRICE_PROVIDER = "bloomberg"
$env:TRADE_RESEARCH_BLOOMBERG_HOST = "10.0.0.1"
$env:TRADE_RESEARCH_BLOOMBERG_PORT = "9100"
```

Alternatively, set these in a JSON config file and point to it:
```powershell
$env:TRADE_RESEARCH_CONFIG = "C:\path\to\config.json"
```
```json
{ "price_provider": "bloomberg", "bloomberg_host": "localhost", "bloomberg_port": 8194 }
```

### blpapi dependency

blpapi is an **optional** dependency not included in the default install. It must be installed separately:

```powershell
# Proper install via Jefferies Artifactory (requires VPN)
.\.venv\Scripts\pip.exe install "blpapi>=3.24" `
  --index-url https://jfrog.corp.jefco.com/artifactory/api/pypi/Python-remote-bloomberg/simple/ `
  --trusted-host jfrog.corp.jefco.com
```

If blpapi is already installed on the system Python but not in `.venv`, copy it across as a workaround:
```powershell
# Find where system Python installed it
python -c "import blpapi, os; print(os.path.dirname(blpapi.__file__))"

# Copy into .venv (replace <path> with the output above)
Copy-Item -Recurse "<path>\blpapi" ".venv\Lib\site-packages\blpapi"
```

The provider raises `OptionalProviderDependencyError` with the install command if blpapi is missing at runtime — the error appears in the CLI output as `failure_category: provider_configuration`.

**Files affected by this step:** `pyproject.toml` (optional dep declaration), `.venv` (runtime).

---

## Verification

```powershell
# Unit tests — no Bloomberg Terminal required
.\.venv\Scripts\python.exe -m pytest tests/test_bloomberg_provider.py -v

# Full suite
.\.venv\Scripts\python.exe -m pytest

# End-to-end with Bloomberg Terminal running
$env:TRADE_RESEARCH_PRICE_PROVIDER = "bloomberg"
.\.venv\Scripts\trade-research.exe run-skill technical AAPL --market US
```
