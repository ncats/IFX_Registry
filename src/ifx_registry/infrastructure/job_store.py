"""SQLite persistence for acquisition job state."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ifx_registry.application.ports.jobs import AcquisitionJobStore
from ifx_registry.domain.errors import (
    AcquisitionAlreadyRunningError,
    AcquisitionJobNotFoundError,
    OperationalStateUnavailableError,
)
from ifx_registry.domain.jobs import AcquisitionJob, AcquisitionStatus
from ifx_registry.domain.models import DatasetId


class SQLiteAcquisitionJobStore(AcquisitionJobStore):
    def __init__(self, database_path: Path):
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._mark_interrupted_jobs_failed()

    def add(self, job: AcquisitionJob) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO acquisition_jobs (
                        job_id, source, dataset, expected_version, status, stage, message,
                        snapshot_id, error, created_at, started_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._values(job),
                )
        except sqlite3.IntegrityError as error:
            active_job = self.find_active(job.dataset)
            if active_job is not None:
                raise AcquisitionAlreadyRunningError(
                    f"Acquisition {active_job.job_id} is already active for {job.dataset}"
                ) from error
            raise

    def get(self, job_id: str) -> AcquisitionJob:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM acquisition_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise AcquisitionJobNotFoundError(f"Acquisition job not found: {job_id}")
        return self._from_row(row)

    def save(self, job: AcquisitionJob) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE acquisition_jobs SET
                    source = ?, dataset = ?, expected_version = ?, status = ?, stage = ?,
                    message = ?, snapshot_id = ?, error = ?, created_at = ?, started_at = ?,
                    updated_at = ?, completed_at = ?
                WHERE job_id = ?
                """,
                (*self._values(job)[1:], job.job_id),
            )
        if cursor.rowcount == 0:
            raise AcquisitionJobNotFoundError(f"Acquisition job not found: {job.job_id}")

    def list_recent(self, *, limit: int = 20) -> tuple[AcquisitionJob, ...]:
        if limit <= 0:
            raise ValueError("job list limit must be positive")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM acquisition_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_active(self) -> tuple[AcquisitionJob, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM acquisition_jobs
                WHERE status IN (?, ?)
                ORDER BY created_at DESC
                """,
                (AcquisitionStatus.QUEUED.value, AcquisitionStatus.RUNNING.value),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_recent_successful(
        self,
        dataset: DatasetId,
        *,
        limit: int = 5,
    ) -> tuple[AcquisitionJob, ...]:
        if limit <= 0:
            raise ValueError("successful job list limit must be positive")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM acquisition_jobs
                WHERE source = ? AND dataset = ? AND status = ?
                ORDER BY completed_at DESC LIMIT ?
                """,
                (
                    dataset.source,
                    dataset.dataset,
                    AcquisitionStatus.SUCCEEDED.value,
                    limit,
                ),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def find_active(self, dataset: DatasetId) -> AcquisitionJob | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM acquisition_jobs
                WHERE source = ? AND dataset = ? AND status IN (?, ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    dataset.source,
                    dataset.dataset,
                    AcquisitionStatus.QUEUED.value,
                    AcquisitionStatus.RUNNING.value,
                ),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._connect() as connection:
                yield connection
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as error:
            raise OperationalStateUnavailableError(
                "Registry operational state is temporarily unavailable"
            ) from error

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS acquisition_jobs (
                    job_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    expected_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    snapshot_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_acquisition_per_dataset
                ON acquisition_jobs (source, dataset)
                WHERE status IN ('queued', 'running')
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(acquisition_jobs)")
            }
            if "started_at" not in columns:
                connection.execute("ALTER TABLE acquisition_jobs ADD COLUMN started_at TEXT")

    def _mark_interrupted_jobs_failed(self) -> None:
        interrupted_at = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE acquisition_jobs
                SET status = ?, stage = ?, message = ?, error = ?, updated_at = ?, completed_at = ?
                WHERE status IN (?, ?)
                """,
                (
                    AcquisitionStatus.FAILED.value,
                    "failed",
                    "Acquisition was interrupted",
                    "The Registry service restarted before this acquisition completed.",
                    interrupted_at,
                    interrupted_at,
                    AcquisitionStatus.QUEUED.value,
                    AcquisitionStatus.RUNNING.value,
                ),
            )

    @staticmethod
    def _values(job: AcquisitionJob) -> tuple[str | None, ...]:
        return (
            job.job_id,
            job.dataset.source,
            job.dataset.dataset,
            job.expected_version,
            job.status.value,
            job.stage,
            job.message,
            job.snapshot_id,
            job.error,
            job.created_at.isoformat(),
            job.started_at.isoformat() if job.started_at else None,
            job.updated_at.isoformat(),
            job.completed_at.isoformat() if job.completed_at else None,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AcquisitionJob:
        return AcquisitionJob(
            job_id=str(row["job_id"]),
            dataset=DatasetId(source=str(row["source"]), dataset=str(row["dataset"])),
            expected_version=str(row["expected_version"]),
            status=AcquisitionStatus(str(row["status"])),
            stage=str(row["stage"]),
            message=str(row["message"]),
            snapshot_id=str(row["snapshot_id"]) if row["snapshot_id"] else None,
            error=str(row["error"]) if row["error"] else None,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            started_at=(
                datetime.fromisoformat(str(row["started_at"])) if row["started_at"] else None
            ),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            completed_at=(
                datetime.fromisoformat(str(row["completed_at"])) if row["completed_at"] else None
            ),
        )
