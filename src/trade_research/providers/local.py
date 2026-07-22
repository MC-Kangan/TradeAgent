"""Bounded local providers for user-managed normalized research data."""

from __future__ import annotations

import csv
import hashlib
import math
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, cast

from trade_research.domain import InstrumentId, Observation, Position
from trade_research.providers.contracts import (
    MAX_FUNDAMENTAL_ROWS,
    MAX_LOCAL_BYTES,
    MAX_PRICE_POINTS,
    PricePoint,
    ProviderConfigurationError,
    ProviderContractError,
)


class _BoundedLocalRows:
    def __init__(self, path: Path, *, max_rows: int) -> None:
        self._path = path.resolve()
        self._max_rows = max_rows
        if max_rows < 1:
            raise ProviderConfigurationError("row limit must be positive")

    def _rows(self) -> tuple[dict[str, Any], ...]:
        if not self._path.is_file():
            raise ProviderConfigurationError("local provider file does not exist")
        if self._path.stat().st_size > MAX_LOCAL_BYTES:
            raise ProviderContractError("local provider exceeded the byte limit")
        suffix = self._path.suffix.lower()
        if suffix == ".csv":
            with self._path.open(newline="", encoding="utf-8") as handle:
                rows = tuple(islice(csv.DictReader(handle), self._max_rows + 1))
        elif suffix == ".parquet":
            try:
                import pyarrow.parquet as pq
            except ImportError as error:  # pragma: no cover - pinned production dependency
                raise ProviderConfigurationError("Parquet support requires pyarrow") from error
            parquet = pq.ParquetFile(self._path)
            if parquet.metadata.num_rows > self._max_rows:
                raise ProviderContractError("local provider exceeded the row limit")
            rows = tuple(parquet.read().to_pylist())
        else:
            raise ProviderConfigurationError("local provider files must be CSV or Parquet")
        if len(rows) > self._max_rows:
            raise ProviderContractError("local provider exceeded the row limit")
        return rows


class LocalCsvParquetPriceProvider(_BoundedLocalRows):
    """Read a fixed normalized price schema from a bounded CSV or Parquet file."""

    def __init__(self, path: Path, *, max_rows: int = MAX_PRICE_POINTS) -> None:
        super().__init__(path, max_rows=max_rows)

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        rows = self._rows()
        reference_hash = _reference_hash(self._path)
        source = f"local_{self._path.suffix.lstrip('.').lower()}"
        prices = tuple(
            PricePoint(
                instrument=instrument,
                observed_at=_parse_datetime(_required_text(row, "observed_at")),
                close=_required_number(row, "close"),
                source=source,
                provenance={
                    "provider_kind": source,
                    "snapshot_ref": reference_hash,
                    "reference": _row_reference(row),
                    "vendor_field": "OHLCV" if row.get("open") not in (None, "") else "CLOSE",
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


class LocalCsvParquetFundamentalProvider(_BoundedLocalRows):
    """Read normalized numeric fundamental observations from bounded CSV or Parquet."""

    def __init__(self, path: Path, *, max_rows: int = MAX_FUNDAMENTAL_ROWS) -> None:
        super().__init__(path, max_rows=max_rows)

    def fundamentals(self, instrument: InstrumentId) -> tuple[Observation, ...]:
        rows = self._rows()
        source = f"local_{self._path.suffix.lstrip('.').lower()}"
        snapshot = _reference_hash(self._path)
        return tuple(
            _fundamental_observation(row, instrument, source=source, snapshot=snapshot)
            for row in rows
            if str(row.get("symbol", "")).upper() == instrument.symbol
        )


class ReadOnlySqlPriceProvider:
    """Read only from the fixed ``prices`` table with a hard SQL LIMIT."""

    def __init__(self, database_path: Path, *, max_rows: int = MAX_PRICE_POINTS) -> None:
        self._database_path = database_path.resolve()
        self._max_rows = max_rows

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        columns, rows = _sql_rows(
            self._database_path,
            "prices",
            instrument,
            self._max_rows,
            ("observed_at", "open", "high", "low", "close", "volume"),
            required=("observed_at", "close"),
        )
        reference_hash = _reference_hash(self._database_path)
        return tuple(
            PricePoint(
                instrument=instrument,
                observed_at=_parse_datetime(str(row["observed_at"])),
                close=_required_number(row, "close"),
                source="local_sql",
                provenance={
                    "provider_kind": "local_sql",
                    "snapshot_ref": reference_hash,
                    "reference": _row_reference(row),
                    "vendor_field": "OHLCV" if "open" in columns else "CLOSE",
                },
                open=_optional_number(row.get("open")),
                high=_optional_number(row.get("high")),
                low=_optional_number(row.get("low")),
                volume=_optional_number(row.get("volume")),
            )
            for row in rows
        )


class ReadOnlySqlFundamentalProvider:
    """Read only from the fixed ``fundamentals`` table with a hard SQL LIMIT."""

    _COLUMNS = (
        "metric",
        "value",
        "observed_at",
        "period_role",
        "period_end",
        "period_type",
        "period_ref",
        "prior_period_ref",
        "snapshot_ref",
        "currency",
        "valuation_as_of",
        "vendor_field",
    )

    def __init__(self, database_path: Path, *, max_rows: int = MAX_FUNDAMENTAL_ROWS) -> None:
        self._database_path = database_path.resolve()
        self._max_rows = max_rows

    def fundamentals(self, instrument: InstrumentId) -> tuple[Observation, ...]:
        _, rows = _sql_rows(
            self._database_path,
            "fundamentals",
            instrument,
            self._max_rows,
            self._COLUMNS,
            required=("metric", "value", "observed_at"),
        )
        snapshot = _reference_hash(self._database_path)
        return tuple(
            _fundamental_observation(row, instrument, source="local_sql", snapshot=snapshot)
            for row in rows
        )


class LocalPortfolioProvider:
    """Return supplied positions without writing or logging them."""

    def __init__(self, positions: tuple[Position, ...]) -> None:
        self._positions = positions

    def positions(self) -> tuple[Position, ...]:
        return self._positions


def _sql_rows(
    path: Path,
    table: str,
    instrument: InstrumentId,
    max_rows: int,
    allowed_columns: tuple[str, ...],
    *,
    required: tuple[str, ...],
) -> tuple[frozenset[str], tuple[dict[str, object], ...]]:
    if max_rows < 1:
        raise ProviderConfigurationError("row limit must be positive")
    if not path.is_file():
        raise ProviderConfigurationError("read-only provider database does not exist")
    if path.stat().st_size > MAX_LOCAL_BYTES:
        raise ProviderContractError("local provider exceeded the byte limit")
    uri = f"{path.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        columns = frozenset(
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if not set(required).issubset(columns) or "symbol" not in columns:
            raise ProviderConfigurationError(f"{table} table does not match the fixed schema")
        selected = tuple(column for column in allowed_columns if column in columns)
        query = (
            f"SELECT {', '.join(selected)} FROM {table} "
            "WHERE symbol = ? ORDER BY observed_at ASC LIMIT ?"
        )
        raw_rows = connection.execute(query, (instrument.symbol, max_rows + 1)).fetchall()
    if len(raw_rows) > max_rows:
        raise ProviderContractError("local SQL provider exceeded the row limit")
    return columns, tuple(dict(zip(selected, row, strict=True)) for row in raw_rows)


def _fundamental_observation(
    row: Mapping[str, object], instrument: InstrumentId, *, source: str, snapshot: str
) -> Observation:
    value = _required_number(row, "value")
    if not math.isfinite(value):
        raise ProviderContractError("fundamental value must be finite")
    provenance: dict[str, object] = {
        "provider_kind": source,
        "snapshot_ref": _optional_text(row.get("snapshot_ref")) or snapshot,
        "reference": _row_reference(row),
    }
    for key in (
        "period_role",
        "period_end",
        "period_type",
        "period_ref",
        "prior_period_ref",
        "currency",
        "valuation_as_of",
        "vendor_field",
    ):
        candidate = _optional_text(row.get(key))
        if candidate is not None:
            provenance[key] = candidate
    return Observation(
        instrument=instrument,
        metric=_required_text(row, "metric"),
        value=value,
        source=source,
        observed_at=_parse_datetime(_required_text(row, "observed_at")),
        provenance=provenance,
    )


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderContractError("provider timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ProviderContractError("provider timestamps must include a timezone")
    return parsed


def _required_text(row: Mapping[str, object], key: str) -> str:
    value = _optional_text(row.get(key))
    if value is None:
        raise ProviderContractError(f"provider row is missing {key}")
    return value


def _optional_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text if text else None


def _required_number(row: Mapping[str, object], key: str) -> float:
    try:
        return float(cast(Any, row[key]))
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderContractError(f"provider row has invalid {key}") from error


def _optional_number(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError) as error:
        raise ProviderContractError("provider row has an invalid numeric value") from error


def _reference_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _row_reference(row: Mapping[str, object]) -> str:
    canonical = "\x1f".join(f"{key}={row[key]}" for key in sorted(row))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
