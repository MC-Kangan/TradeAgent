"""Recoverable SQLite job queue for bounded research requests."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from trade_research.domain import AnalysisRequest, ResearchReport
from trade_research.engine import ResearchEngine
from trade_research.reporting import render_json
from trade_research.storage import PersistedAnalysisRequest

JobStatus = Literal["queued", "running", "succeeded", "failed"]


class ResearchJob(BaseModel):
    """A safe status view returned by workers and interfaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: PersistedAnalysisRequest
    status: JobStatus
    result: ResearchReport | None = None
    error_type: str | None = None


class JobQueue:
    """SQLite queue that recovers interrupted running work on construction."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        recovery_after_seconds: float = 300,
    ) -> None:
        self.database_path = Path(database_path)
        self.recovery_after_seconds = max(0, recovery_after_seconds)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, timeout=30)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    request_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
                    result_json TEXT,
                    error_type TEXT,
                    claimed_at REAL
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
            if "claimed_at" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN claimed_at REAL")
            cutoff = time.time() - self.recovery_after_seconds
            connection.execute(
                """
                UPDATE jobs SET status = 'queued', claimed_at = NULL
                WHERE status = 'running' AND (claimed_at IS NULL OR claimed_at <= ?)
                """,
                (cutoff,),
            )

    def enqueue(self, request: AnalysisRequest) -> UUID:
        persisted = PersistedAnalysisRequest.from_request(request)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    request_id, request_json, status, result_json, error_type, claimed_at
                )
                VALUES (?, ?, 'queued', NULL, NULL, NULL)
                ON CONFLICT(request_id) DO NOTHING
                """,
                (str(request.request_id), persisted.model_dump_json()),
            )
        return request.request_id

    def claim_next(self) -> ResearchJob | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_id FROM jobs
                WHERE status = 'queued' ORDER BY rowid LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE jobs SET status = 'running', claimed_at = ?
                WHERE request_id = ? AND status = 'queued'
                """,
                (time.time(), row[0]),
            )
        return self.get(UUID(row[0]))

    def complete(self, request_id: UUID, report: ResearchReport) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'succeeded', result_json = ?,
                    error_type = NULL, claimed_at = NULL
                WHERE request_id = ?
                """,
                (render_json(report), str(request_id)),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown job: {request_id}")

    def fail(self, request_id: UUID, error: Exception) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'failed', result_json = NULL,
                    error_type = ?, claimed_at = NULL
                WHERE request_id = ?
                """,
                (type(error).__name__, str(request_id)),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown job: {request_id}")

    def get(self, request_id: UUID) -> ResearchJob:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_json, status, result_json, error_type
                FROM jobs WHERE request_id = ?
                """,
                (str(request_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown job: {request_id}")
        request = PersistedAnalysisRequest.model_validate_json(row[0])
        result = ResearchReport.model_validate_json(row[2]) if row[2] is not None else None
        return ResearchJob(request=request, status=row[1], result=result, error_type=row[3])

    def list_succeeded(self) -> tuple[UUID, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT request_id FROM jobs WHERE status = 'succeeded' ORDER BY request_id"
            ).fetchall()
        return tuple(UUID(row[0]) for row in rows)


class ResearchWorker:
    """Process at most one queue item per call for deterministic supervision."""

    def __init__(self, queue: JobQueue, engine: ResearchEngine) -> None:
        self._queue = queue
        self._engine = engine

    async def run_once(self) -> bool:
        job = self._queue.claim_next()
        if job is None:
            return False
        try:
            report = await self._engine.analyze(job.request.to_request())
        except Exception as error:
            self._queue.fail(job.request.request_id, error)
        else:
            self._queue.complete(job.request.request_id, report)
        return True
