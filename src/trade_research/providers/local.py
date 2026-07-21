"""Safe local providers for fixtures and user-managed data exports."""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from trade_research.domain import InstrumentId, Position
from trade_research.providers.contracts import PricePoint, ProviderConfigurationError


class LocalCsvParquetPriceProvider:
    """Read a narrow, documented price schema from local CSV or Parquet files."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        rows = self._rows()
        reference_hash = _reference_hash(self._path)
        prices = tuple(
            PricePoint(
                observed_at=_parse_datetime(str(row["observed_at"])),
                close=float(row["close"]),
                source=f"local_{self._path.suffix.lstrip('.').lower()}",
                provenance={
                    "provider_kind": f"local_{self._path.suffix.lstrip('.').lower()}",
                    "reference": reference_hash,
                    "vendor_field": "OHLCV" if row.get("open") is not None else "CLOSE",
                },
                open=_optional_number(row.get("open")),
                high=_optional_number(row.get("high")),
                low=_optional_number(row.get("low")),
                volume=_optional_number(row.get("volume")),
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
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(prices)").fetchall()
            }
            if {"open", "high", "low", "volume"}.issubset(columns):
                rows = connection.execute(
                    "SELECT observed_at, open, high, low, close, volume "
                    "FROM prices WHERE symbol = ? ORDER BY observed_at ASC",
                    (instrument.symbol,),
                ).fetchall()
            else:
                rows = [
                    (observed_at, None, None, None, close, None)
                    for observed_at, close in connection.execute(
                        "SELECT observed_at, close FROM prices "
                        "WHERE symbol = ? ORDER BY observed_at ASC",
                        (instrument.symbol,),
                    ).fetchall()
                ]
        reference_hash = _reference_hash(self._database_path)
        return tuple(
            PricePoint(
                observed_at=_parse_datetime(str(observed_at)),
                close=float(close),
                source="local_sql",
                provenance={
                    "provider_kind": "local_sql",
                    "reference": reference_hash,
                    "vendor_field": "OHLCV" if open_value is not None else "CLOSE",
                },
                open=_optional_number(open_value),
                high=_optional_number(high),
                low=_optional_number(low),
                volume=_optional_number(volume),
            )
            for observed_at, open_value, high, low, close, volume in rows
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


def _optional_number(value: object) -> float | None:
    return None if value in (None, "") else float(cast(Any, value))


def _reference_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
