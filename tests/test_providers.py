from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from trade_research.domain import InstrumentId, Position
from trade_research.providers import (
    LocalCsvParquetPriceProvider,
    LocalPortfolioProvider,
    ProviderRegistry,
    ReadOnlySqlPriceProvider,
    StooqPriceProvider,
    YahooPriceProvider,
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

    def stooq_get(url: str, headers: object) -> str:
        requested.append(url)
        return "Date,Open,High,Low,Close,Volume\n2026-01-01,99,102,98,101,1000\n"

    yahoo = YahooPriceProvider(http_get=yahoo_get).price_history(
        InstrumentId(symbol="VOD", market="UK")
    )[0]
    stooq = StooqPriceProvider(http_get=stooq_get).price_history(
        InstrumentId(symbol="SAP", market="XETRA")
    )[0]

    assert (yahoo.open, yahoo.high, yahoo.low, yahoo.close, yahoo.volume) == (
        99.0,
        102.0,
        98.0,
        101.0,
        1000.0,
    )
    assert (stooq.open, stooq.high, stooq.low, stooq.close, stooq.volume) == (
        99.0,
        102.0,
        98.0,
        101.0,
        1000.0,
    )
    assert "VOD.L" in requested[0]
    assert "sap.de" in requested[1]


def test_portfolio_provider_keeps_positions_in_memory() -> None:
    position = Position(instrument=InstrumentId(symbol="ACME", market="NYSE"), quantity=2)

    provider = LocalPortfolioProvider((position,))

    assert provider.positions() == (position,)


def test_provider_registry_is_immutable_and_requires_known_provider() -> None:
    registry = ProviderRegistry({"prices": object()})

    assert registry.require("prices") is registry.providers["prices"]
    with pytest.raises(KeyError, match="prices_missing"):
        registry.require("prices_missing")
    with pytest.raises(TypeError):
        registry.providers["other"] = object()  # type: ignore[index]
