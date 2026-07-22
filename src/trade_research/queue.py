"""Recoverable, lease-fenced SQLite job queue for bounded research requests."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from trade_research.domain import AnalysisRequest, ResearchReport
from trade_research.engine import ResearchEngine
from trade_research.reporting import render_json
from trade_research.storage import PersistedAnalysisRequest

JobStatus = Literal["queued", "running", "succeeded", "failed"]


class LostLease(RuntimeError):
    """Raised when a stale worker attempts to mutate a job it no longer owns."""


class RequestConflict(RuntimeError):
    """Raised when a request UUID already names a different safe request."""

    def __init__(self, request_id: UUID) -> None:
        self.request_id = request_id
        super().__init__("request id conflicts with an existing request")


class QueueSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    status: JobStatus


class ResearchJob(BaseModel):
    """A safe job view; running claims include their fencing identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: PersistedAnalysisRequest
    status: JobStatus
    result: ResearchReport | None = None
    error_type: str | None = None
    claim_token: UUID | None = None
    generation: int = 0


class JobQueue:
    """SQLite queue with atomic expiry reaping and fenced terminal writes."""

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
                    claimed_at REAL,
                    claim_token TEXT,
                    generation INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
            if "claimed_at" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN claimed_at REAL")
            if "claim_token" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN claim_token TEXT")
            if "generation" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
                )

    def enqueue(self, request: AnalysisRequest) -> QueueSubmission:
        persisted = PersistedAnalysisRequest.from_request(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_json, status FROM jobs WHERE request_id = ?",
                (str(persisted.request_id),),
            ).fetchone()
            if row is not None:
                existing = PersistedAnalysisRequest.model_validate_json(row[0])
                if existing != persisted:
                    raise RequestConflict(persisted.request_id)
                return QueueSubmission(request_id=persisted.request_id, status=row[1])
            connection.execute(
                """
                INSERT INTO jobs (
                    request_id, request_json, status, result_json, error_type,
                    claimed_at, claim_token, generation
                ) VALUES (?, ?, 'queued', NULL, NULL, NULL, NULL, 0)
                """,
                (str(persisted.request_id), persisted.model_dump_json()),
            )
        return QueueSubmission(request_id=persisted.request_id, status="queued")

    def claim_next(self) -> ResearchJob | None:
        now = time.time()
        cutoff = now - self.recovery_after_seconds
        claim_token = uuid4()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE jobs SET status = 'queued', claimed_at = NULL, claim_token = NULL
                WHERE status = 'running' AND (claimed_at IS NULL OR claimed_at <= ?)
                """,
                (cutoff,),
            )
            row = connection.execute(
                "SELECT request_id FROM jobs WHERE status = 'queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE jobs SET status = 'running', claimed_at = ?, claim_token = ?,
                    generation = generation + 1
                WHERE request_id = ? AND status = 'queued'
                """,
                (now, str(claim_token), row[0]),
            )
            claimed = connection.execute(
                "SELECT request_json, generation FROM jobs WHERE request_id = ?",
                (row[0],),
            ).fetchone()
            if claimed is None:
                raise LostLease("claimed job disappeared during its transaction")
            job = ResearchJob(
                request=PersistedAnalysisRequest.model_validate_json(claimed[0]),
                status="running",
                claim_token=claim_token,
                generation=claimed[1],
            )
        return job

    def complete(self, request_id: UUID, report: ResearchReport, claim_token: UUID) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'succeeded', result_json = ?, error_type = NULL,
                    claimed_at = NULL, claim_token = NULL
                WHERE request_id = ? AND status = 'running' AND claim_token = ?
                """,
                (render_json(report), str(request_id), str(claim_token)),
            )
            if cursor.rowcount != 1:
                raise LostLease("job lease is no longer owned by this worker")

    def fail(self, request_id: UUID, error: Exception, claim_token: UUID) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'failed', result_json = NULL, error_type = ?,
                    claimed_at = NULL, claim_token = NULL
                WHERE request_id = ? AND status = 'running' AND claim_token = ?
                """,
                (type(error).__name__, str(request_id), str(claim_token)),
            )
            if cursor.rowcount != 1:
                raise LostLease("job lease is no longer owned by this worker")

    def get(self, request_id: UUID) -> ResearchJob:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_json, status, result_json, error_type, claim_token, generation
                FROM jobs WHERE request_id = ?
                """,
                (str(request_id),),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown job: {request_id}")
        request = PersistedAnalysisRequest.model_validate_json(row[0])
        result = ResearchReport.model_validate_json(row[2]) if row[2] is not None else None
        token = UUID(row[4]) if row[4] is not None else None
        return ResearchJob(
            request=request,
            status=row[1],
            result=result,
            error_type=row[3],
            claim_token=token,
            generation=row[5],
        )

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
        if job.claim_token is None:
            raise LostLease("claimed job has no fencing token")
        try:
            report = await self._engine.analyze(job.request.to_request())
        except Exception as error:
            self._queue.fail(job.request.request_id, error, job.claim_token)
        else:
            self._queue.complete(job.request.request_id, report, job.claim_token)
        return True
