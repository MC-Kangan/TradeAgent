"""SQLite persistence for redacted analysis requests."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from trade_research.domain import AnalysisRequest

_SENSITIVE_KEY_PARTS = (
    "account",
    "api_key",
    "apikey",
    "authorization",
    "client_ip",
    "cookie",
    "password",
    "secret",
    "token",
)


def _without_sensitive_values(value: Any) -> Any:
    """Remove values that must never cross the persisted-request boundary."""
    if isinstance(value, Mapping):
        return {
            key: _without_sensitive_values(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_without_sensitive_values(item) for item in value]
    return value


class RunStore:
    """Persist sanitized request records in a WAL-enabled SQLite database."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    request_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL
                )
                """
            )

    def save_request(self, request: AnalysisRequest) -> None:
        """Store a request after dropping raw positions and sensitive metadata."""
        payload = request.model_dump(mode="json", exclude={"positions"})
        redacted_payload = _without_sensitive_values(payload)
        serialized = json.dumps(redacted_payload, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs (request_id, request_json) VALUES (?, ?)",
                (str(request.request_id), serialized),
            )

    def load_request(self, request_id: UUID) -> AnalysisRequest:
        """Load a previously stored, already-redacted request."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM runs WHERE request_id = ?", (str(request_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown request id: {request_id}")
        return AnalysisRequest.model_validate_json(row[0])
