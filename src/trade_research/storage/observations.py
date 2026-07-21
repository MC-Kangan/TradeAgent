"""Parquet persistence for structured market observations."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq

from trade_research.domain import Observation


class ObservationStore:
    """Write and read a run's observations as portable Parquet data."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, run_id: UUID | str) -> Path:
        return self.root / f"{run_id}.parquet"

    def save(self, run_id: UUID | str, observations: Iterable[Observation]) -> Path:
        """Write the given observations under one run identifier."""
        path = self.path_for(run_id)
        rows = [observation.model_dump_json() for observation in observations]
        pq.write_table(pa.table({"observation_json": rows}), path)
        return path

    def load(self, run_id: UUID | str) -> tuple[Observation, ...]:
        """Read the observations stored for one run identifier."""
        table = pq.read_table(self.path_for(run_id), columns=["observation_json"])
        serialized = table.column("observation_json").to_pylist()
        if any(value is None for value in serialized):
            raise ValueError("observation parquet contains a null serialized observation")
        return tuple(Observation.model_validate_json(str(value)) for value in serialized)
