from __future__ import annotations

import csv
import json
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from trade_research.domain import InstrumentId, Position
from trade_research.providers import (
    BloombergPriceProvider,
    CikResolver,
    LocalCsvParquetPriceProvider,
    LocalPortfolioProvider,
    OptionalProviderDependencyError,
    ProviderConfigurationError,
    ProviderRegistry,
    ReadOnlySqlPriceProvider,
    SecCompanyFactsProvider,
    YahooPriceProvider,
    resolve_provider_symbol,
)


def test_local_csv_provider_returns_provenance_bearing_prices(tmp_path: Path) -> None:
    path = tmp_path / "prices.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("symbol", "observed_at", "close"))
        writer.writeheader()
        writer.writerow(
            {"symbol": "ACME", "observed_at": "2026-01-02T00:00:00+00:00", "close": "100"}
        )

    prices = LocalCsvParquetPriceProvider(path).price_history(
        InstrumentId(symbol="ACME", market="NASDAQ")
    )

    assert [price.close for price in prices] == [100.0]
    assert prices[0].source == "local_csv"
    assert prices[0].provenance["provider_kind"] == "local_csv"
    assert prices[0].provenance["vendor_field"] == "CLOSE"
    assert prices[0].provenance["reference"].startswith("sha256:")
    assert len(prices[0].provenance["reference"]) == 71
    assert str(path) not in json.dumps(dict(prices[0].provenance))


def test_local_csv_provider_reads_complete_ohlcv_when_present(tmp_path: Path) -> None:
    path = tmp_path / "ohlcv.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("symbol", "observed_at", "open", "high", "low", "close", "volume"),
        )
        writer.writeheader()
        writer.writerow(
            {
                "symbol": "ACME",
                "observed_at": "2026-01-02T00:00:00+00:00",
                "open": "99",
                "high": "102",
                "low": "98",
                "close": "101",
                "volume": "1000",
            }
        )

    point = LocalCsvParquetPriceProvider(path).price_history(
        InstrumentId(symbol="ACME", market="US")
    )[0]

    assert (point.open, point.high, point.low, point.close, point.volume) == (
        99.0,
        102.0,
        98.0,
        101.0,
        1000.0,
    )


def test_local_provider_uses_provider_kind_and_hash_not_account_bearing_path(
    tmp_path: Path,
) -> None:
    account_directory = tmp_path / "Users" / "alice-account" / "private"
    account_directory.mkdir(parents=True)
    path = account_directory / "prices.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("symbol", "observed_at", "close"))
        writer.writeheader()
        writer.writerow(
            {"symbol": "ACME", "observed_at": "2026-01-02T00:00:00+00:00", "close": "100"}
        )

    point = LocalCsvParquetPriceProvider(path).price_history(
        InstrumentId(symbol="ACME", market="US")
    )[0]
    payload = json.dumps(dict(point.provenance), sort_keys=True)

    assert point.provenance["provider_kind"] == "local_csv"
    assert point.provenance["reference"].startswith("sha256:")
    assert len(point.provenance["reference"]) == 71
    assert "alice-account" not in payload
    assert str(path) not in payload


def test_sql_provider_uses_a_fixed_parameterized_read_only_query(tmp_path: Path) -> None:
    database = tmp_path / "prices.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE prices (symbol TEXT, observed_at TEXT, close REAL)")
        connection.execute(
            "INSERT INTO prices VALUES (?, ?, ?)",
            ("ACME", "2026-01-02T00:00:00+00:00", 101.5),
        )

    provider = ReadOnlySqlPriceProvider(database)

    instrument = InstrumentId(symbol="ACME", market="NASDAQ")
    assert [price.close for price in provider.price_history(instrument)] == [101.5]
    assert not hasattr(provider, "query")


def test_sql_provider_reads_fixed_ohlcv_schema_when_available(tmp_path: Path) -> None:
    database = tmp_path / "ohlcv.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE prices ("
            "symbol TEXT, observed_at TEXT, open REAL, high REAL, low REAL, "
            "close REAL, volume REAL)"
        )
        connection.execute(
            "INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ACME", "2026-01-02T00:00:00+00:00", 99, 102, 98, 101, 1000),
        )

    point = ReadOnlySqlPriceProvider(database).price_history(
        InstrumentId(symbol="ACME", market="US")
    )[0]

    assert (point.open, point.high, point.low, point.close, point.volume) == (
        99.0,
        102.0,
        98.0,
        101.0,
        1000.0,
    )


def test_remote_price_adapters_parse_ohlcv_and_resolved_symbols() -> None:
    requested: list[str] = []

    def yahoo_get(url: str, headers: object) -> str:
        requested.append(url)
        return (
            '{"chart":{"result":[{"timestamp":[1767225600],"indicators":{"quote":[{'
            '"open":[99],"high":[102],"low":[98],"close":[101],"volume":[1000]}]}}]}}'
        )

    yahoo = YahooPriceProvider(http_get=yahoo_get).price_history(
        InstrumentId(symbol="VOD", market="UK")
    )[0]

    assert (yahoo.open, yahoo.high, yahoo.low, yahoo.close, yahoo.volume) == (
        99.0,
        102.0,
        98.0,
        101.0,
        1000.0,
    )
    assert "VOD.L" in requested[0]


def test_yahoo_price_adapter_supports_bounded_chart_windows() -> None:
    requested: list[str] = []

    def yahoo_get(url: str, headers: object) -> str:
        requested.append(url)
        return (
            '{"chart":{"result":[{"timestamp":[1767225600],"indicators":{"quote":[{'
            '"open":[99],"high":[102],"low":[98],"close":[101],"volume":[1000]}]}}]}}'
        )

    YahooPriceProvider(
        http_get=yahoo_get,
        range_="10y",
        interval="1d",
    ).price_history(InstrumentId(symbol="^GSPC", market="INDEX"))
    YahooPriceProvider(
        http_get=yahoo_get,
        range_="60d",
        interval="30m",
    ).price_history(InstrumentId(symbol="^GSPC", market="INDEX"))

    assert "range=10y&interval=1d" in requested[0]
    assert "range=60d&interval=30m" in requested[1]


def test_yahoo_price_adapter_rejects_unbounded_chart_parameters() -> None:
    with pytest.raises(ValueError, match="unsupported Yahoo chart window"):
        YahooPriceProvider(range_="max", interval="1m")


def test_sec_company_facts_derives_only_registry_valid_free_cash_flow(tmp_path: Path) -> None:
    instrument = InstrumentId(symbol="ACME", market="US")
    payload = {
        "entityName": "Acme Example",
        "facts": {
            "us-gaap": {
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            {
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "val": 100.0,
                                "end": "2025-12-31",
                                "accn": "0000000000-26-000001",
                                "frame": "CY2025",
                            }
                        ]
                    }
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            {
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "val": -20.0,
                                "end": "2025-12-31",
                                "accn": "0000000000-26-000002",
                                "frame": "CY2025",
                            }
                        ]
                    }
                },
            }
        },
    }
    resolver = CikResolver(
        user_agent="research@example.test",
        cache_dir=tmp_path,
        overrides={"ACME": "0000000001"},
    )
    provider = SecCompanyFactsProvider(
        resolver=resolver,
        user_agent="research@example.test",
        http_get=lambda _url, _headers: json.dumps(payload),
    )

    observations = ProviderRegistry({"fundamentals": provider}).fundamentals(instrument)

    assert [(item.metric.value, item.value) for item in observations] == [
        ("free_cash_flow", 80.0)
    ]
    fcf = observations[0]
    assert fcf.provenance["period_end"] == "2025-12-31"
    assert fcf.provenance["period_role"] == "current"
    assert str(fcf.provenance["period_ref"]).startswith("sha256:")
    assert str(fcf.provenance["reference"]).startswith("sha256:")


def test_sec_cik_resolver_uses_stale_valid_cache_when_refresh_fails(tmp_path: Path) -> None:
    (tmp_path / "sec_cik_map.json").write_text(
        json.dumps(
            {
                "retrieved_at": "2020-01-01T00:00:00+00:00",
                "content_hash": "sha256:cached",
                "mapping": {"ACME": "0000000001"},
            }
        ),
        encoding="utf-8",
    )

    def fail_refresh(_url, _headers):
        raise ProviderConfigurationError("SEC unavailable")

    resolver = CikResolver(
        "research@example.test",
        tmp_path,
        http_get=fail_refresh,
    )

    assert resolver.resolve("ACME") == "0000000001"
    assert resolver.mapping_hash == "sha256:cached"


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        ("US", "AAPL US Equity"),
        ("NASDAQ", "AAPL US Equity"),
        ("LSE", "AAPL LN Equity"),
        ("UK", "AAPL LN Equity"),
        ("XETRA", "AAPL GY Equity"),
        ("BORSA_ITALIANA", "AAPL IM Equity"),
    ],
)
def test_bloomberg_symbol_resolution_for_supported_markets(
    market: str, expected: str
) -> None:
    assert (
        resolve_provider_symbol("bloomberg", InstrumentId(symbol="AAPL", market=market))
        == expected
    )


def test_bloomberg_symbol_resolution_rejects_crypto() -> None:
    with pytest.raises(ProviderConfigurationError, match="does not support market"):
        resolve_provider_symbol("bloomberg", InstrumentId(symbol="BTC-USD", market="CRYPTO"))


def test_bloomberg_provider_requires_optional_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "blpapi", None)

    with pytest.raises(OptionalProviderDependencyError, match="optional 'blpapi' dependency"):
        BloombergPriceProvider().price_history(InstrumentId(symbol="AAPL", market="US"))


def test_bloomberg_provider_reuses_session_and_returns_ohlcv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = InstrumentId(symbol="AAPL", market="US")
    session = _FakeBloombergSession(
        rows=[
            {
                "date": "2026-01-02",
                "PX_OPEN": 99.0,
                "PX_HIGH": 102.0,
                "PX_LOW": 98.0,
                "PX_LAST": 101.0,
                "PX_VOLUME": 1000.0,
            }
        ]
    )
    monkeypatch.setitem(sys.modules, "blpapi", _fake_blpapi_module(session))

    provider = BloombergPriceProvider()
    first = provider.price_history(instrument)
    second = provider.price_history(instrument)

    assert session.start.call_count == 1
    assert first == second
    point = first[0]
    assert (point.open, point.high, point.low, point.close, point.volume) == (
        99.0,
        102.0,
        98.0,
        101.0,
        1000.0,
    )
    assert point.source == "bloomberg"
    assert point.provenance["provider_kind"] == "bloomberg"
    assert point.provenance["vendor_field"] == "OHLCV"
    assert str(point.provenance["reference"]).startswith("sha256:")
    assert str(point.provenance["snapshot_ref"]).startswith("sha256:")


def test_bloomberg_provider_raises_on_response_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeBloombergSession(rows=[], response_error=True)
    monkeypatch.setitem(sys.modules, "blpapi", _fake_blpapi_module(session))

    with pytest.raises(ProviderConfigurationError, match="Bloomberg returned an error"):
        BloombergPriceProvider().price_history(InstrumentId(symbol="AAPL", market="US"))


def test_portfolio_provider_keeps_positions_in_memory() -> None:
    position = Position(instrument=InstrumentId(symbol="ACME", market="NYSE"), quantity=2)

    provider = LocalPortfolioProvider((position,))

    assert provider.positions() == (position,)


def test_provider_registry_is_immutable_and_requires_known_provider() -> None:
    class Prices:
        def price_history(self, instrument: InstrumentId) -> tuple[object, ...]:
            del instrument
            return ()

    registry = ProviderRegistry({"prices": Prices()})  # type: ignore[dict-item]

    assert registry.require("prices") is registry.providers["prices"]
    with pytest.raises(ProviderConfigurationError, match="prices_missing"):
        registry.require("prices_missing")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        registry.providers["other"] = Prices()  # type: ignore[index]


class _FakeBloombergEvent:
    RESPONSE = 1
    PARTIAL_RESPONSE = 2
    TIMEOUT = 3


class _FakeMessage:
    def __init__(self, rows: list[dict[str, object]], *, response_error: bool = False) -> None:
        self._payload = {
            "securityData": {
                "fieldData": rows,
            }
        }
        if response_error:
            self._payload["responseError"] = {"message": "fictional error"}

    def hasElement(self, name: str) -> bool:
        return name in self._payload

    def getElement(self, name: str) -> object:
        return self._payload[name]


class _FakeEvent:
    def __init__(self, rows: list[dict[str, object]], *, response_error: bool = False) -> None:
        self._messages = [_FakeMessage(rows, response_error=response_error)]

    def eventType(self) -> int:
        return _FakeBloombergEvent.RESPONSE

    def __iter__(self) -> object:
        return iter(self._messages)


class _FakeService:
    def createRequest(self, name: str) -> object:
        assert name == "HistoricalDataRequest"
        return _FakeRequest()


class _FakeRequest:
    def __init__(self) -> None:
        self.fields: list[str] = []
        self.values: dict[str, object] = {}

    def append(self, key: str, value: object) -> None:
        self.values.setdefault(key, [])
        assert isinstance(self.values[key], list)
        self.values[key].append(value)

    def set(self, key: str, value: object) -> None:
        self.values[key] = value


class _FakeBloombergSession:
    def __init__(self, rows: list[dict[str, object]], *, response_error: bool = False) -> None:
        self._rows = rows
        self._response_error = response_error
        self.start = Mock(return_value=True)
        self.openService = Mock(return_value=True)
        self.getService = Mock(return_value=_FakeService())
        self.stop = Mock()
        self.sendRequest = Mock()

    def nextEvent(self, timeout: int) -> _FakeEvent:
        assert timeout == 30000
        return _FakeEvent(self._rows, response_error=self._response_error)


def _fake_blpapi_module(session: _FakeBloombergSession) -> types.ModuleType:
    module = types.ModuleType("blpapi")
    module.Event = _FakeBloombergEvent
    module.SessionOptions = Mock(return_value=Mock())
    module.Session = Mock(return_value=session)
    return module
