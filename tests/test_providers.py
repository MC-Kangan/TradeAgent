from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from trade_research.domain import InstrumentId, Position
from trade_research.providers import (
    LocalCsvParquetPriceProvider,
    LocalPortfolioProvider,
    ProviderRegistry,
    ReadOnlySqlPriceProvider,
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
    assert prices[0].source == "local-csv"
    assert prices[0].provenance == {"path": str(path)}


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
