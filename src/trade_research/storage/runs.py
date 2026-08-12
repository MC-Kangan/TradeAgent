"""SQLite persistence for safe analysis-request scheduling data."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from trade_research.domain import AnalysisRequest, InstrumentId


class PersistedAnalysisRequest(BaseModel):
    """The complete allow-list of fields permitted in persisted job inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    instrument: InstrumentId
    analysts: tuple[str, ...]
    scope: Literal["instrument", "portfolio"] = "instrument"
    portfolio_instruments: tuple[InstrumentId, ...] = ()
    skill_parameters: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)

    @classmethod
    def from_request(cls, request: AnalysisRequest) -> PersistedAnalysisRequest:
        validated = AnalysisRequest.model_validate(request.model_dump())
        safe_parameters: dict[str, dict[str, JsonValue]] = {}
        if validated.scope == "portfolio":
            for name in validated.analysts:
                supplied = validated.skill_parameters.get(name, {})
                safe_parameters[name] = {
                    key: value for key, value in supplied.items() if key in {"lookback", "method"}
                }
        return cls(
            request_id=validated.request_id,
            instrument=validated.instrument,
            analysts=validated.analysts,
            scope=validated.scope,
            portfolio_instruments=validated.portfolio_instruments,
            skill_parameters=safe_parameters,
        )

    def to_request(self) -> AnalysisRequest:
        return AnalysisRequest(
            request_id=self.request_id,
            instrument=self.instrument,
            analysts=self.analysts,
            scope=self.scope,
            portfolio_instruments=self.portfolio_instruments,
            skill_parameters=self.skill_parameters,
        )


class RunStore:
    """Persist only safe request scheduling fields in a WAL-enabled database."""

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
        """Store the fixed safe DTO, never free-form metadata or positions."""
        serialized = PersistedAnalysisRequest.from_request(request).model_dump_json(
            exclude_defaults=True
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs (request_id, request_json) VALUES (?, ?)",
                (str(request.request_id), serialized),
            )

    def load_request(self, request_id: UUID) -> AnalysisRequest:
        """Load a request with empty metadata and positions by construction."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM runs WHERE request_id = ?", (str(request_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown request id: {request_id}")
        return PersistedAnalysisRequest.model_validate_json(row[0]).to_request()
