from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from trade_research.domain import AnalysisRequest, InstrumentId, Observation, Position
from trade_research.storage import ObservationStore, RunStore


def test_run_store_enables_wal_and_persists_secret_free_request(tmp_path: Path) -> None:
    database_path = tmp_path / "runs.sqlite3"
    request = AnalysisRequest(
        instrument=InstrumentId(symbol="ACME", market="NASDAQ"),
        metadata={"api_key": "do-not-persist", "strategy": "long-term"},
        positions=(
            Position(
                instrument=InstrumentId(symbol="ACME", market="NASDAQ"),
                quantity=2,
                average_cost=100,
            ),
        ),
    )
    store = RunStore(database_path)

    store.save_request(request)

    with sqlite3.connect(database_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        payload = json.loads(connection.execute("SELECT request_json FROM runs").fetchone()[0])

    assert journal_mode.lower() == "wal"
    assert payload["metadata"] == {"strategy": "long-term"}
    assert "positions" not in payload
    assert "do-not-persist" not in json.dumps(payload)
    assert store.load_request(request.request_id).instrument.symbol == "ACME"


def test_observation_store_round_trips_parquet(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path / "observations")
    run_id = uuid4()
    observations = (
        Observation(
            instrument=InstrumentId(symbol="ACME", market="NASDAQ"),
            metric="close",
            value=123.45,
            source="fixture",
            observed_at=datetime(2026, 7, 21, tzinfo=UTC),
            provenance={"dataset": "prices"},
        ),
    )

    path = store.save(run_id, observations)

    assert path.suffix == ".parquet"
    assert store.load(run_id) == observations
