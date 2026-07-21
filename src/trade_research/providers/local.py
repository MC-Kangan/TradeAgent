"""Safe local providers for fixtures and user-managed data exports."""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from trade_research.domain import InstrumentId, Position
from trade_research.providers.contracts import PricePoint, ProviderConfigurationError


class LocalCsvParquetPriceProvider:
    """Read a narrow, documented price schema from local CSV or Parquet files."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        rows = self._rows()
        prices = tuple(
            PricePoint(
                observed_at=_parse_datetime(str(row["observed_at"])),
                close=float(row["close"]),
                source=f"local-{self._path.suffix.lstrip('.').lower()}",
                provenance={"path": str(self._path)},
            )
            for row in rows
            if str(row.get("symbol", "")).upper() == instrument.symbol
        )
        return tuple(sorted(prices, key=lambda point: point.observed_at))

    def _rows(self) -> Iterable[dict[str, Any]]:
        suffix = self._path.suffix.lower()
        if suffix == ".csv":
            with self._path.open(newline="", encoding="utf-8") as handle:
                return tuple(csv.DictReader(handle))
        if suffix == ".parquet":
            try:
                import pyarrow.parquet as pq
            except ImportError as error:  # pragma: no cover - pinned production dependency
                raise ProviderConfigurationError("Parquet support requires pyarrow") from error
            return tuple(pq.read_table(self._path).to_pylist())
        raise ProviderConfigurationError("local price files must be CSV or Parquet")


class ReadOnlySqlPriceProvider:
    """Read only from the fixed ``prices`` table using a parameterized query."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.resolve()

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        if not self._database_path.is_file():
            raise ProviderConfigurationError("read-only price database does not exist")
        uri = f"{self._database_path.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                "SELECT observed_at, close FROM prices WHERE symbol = ? ORDER BY observed_at ASC",
                (instrument.symbol,),
            ).fetchall()
        return tuple(
            PricePoint(
                observed_at=_parse_datetime(str(observed_at)),
                close=float(close),
                source="local-sql",
                provenance={"database": str(self._database_path), "table": "prices"},
            )
            for observed_at, close in rows
        )


class LocalPortfolioProvider:
    """Return supplied positions without writing or logging them."""

    def __init__(self, positions: tuple[Position, ...]) -> None:
        self._positions = positions

    def positions(self) -> tuple[Position, ...]:
        return self._positions


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ProviderConfigurationError("price timestamps must include a timezone")
    return parsed
