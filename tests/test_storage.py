from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from trade_research.domain import AnalysisRequest, InstrumentId, Observation, Position
from trade_research.storage import ObservationStore, RunStore


def test_run_store_enables_wal_and_persists_only_safe_request_dto(tmp_path: Path) -> None:
    database_path = tmp_path / "runs.sqlite3"
    request = AnalysisRequest(
        instrument=InstrumentId(symbol="ACME", market="NASDAQ"),
        metadata={
            "api_key": "do-not-persist",
            "clientIp": "203.0.113.10",
            "ip_address": "203.0.113.11",
            "account": "account-123",
            "portfolio": [{"quantity": 7}],
            "positions": [{"quantity": 8}],
            "strategy": "secret-value",
        },
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
    assert payload == {
        "analysts": ["fundamental", "technical"],
        "instrument": {"market": "NASDAQ", "symbol": "ACME"},
        "request_id": str(request.request_id),
    }
    serialized_payload = json.dumps(payload)
    for sensitive_value in (
        "do-not-persist",
        "203.0.113.10",
        "203.0.113.11",
        "account-123",
        "secret-value",
    ):
        assert sensitive_value not in serialized_payload

    loaded_request = store.load_request(request.request_id)
    assert loaded_request.instrument.symbol == "ACME"
    assert loaded_request.metadata == {}
    assert loaded_request.positions == ()


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


@pytest.mark.parametrize("run_id", ["/tmp/target", "../outside", "not-a-uuid"])
def test_observation_store_rejects_non_uuid_run_ids(tmp_path: Path, run_id: str) -> None:
    store = ObservationStore(tmp_path / "observations")

    with pytest.raises(ValueError, match="valid UUID"):
        store.path_for(run_id)
