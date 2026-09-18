"""SQLite persistence for Registry-managed derived build jobs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ifx_registry.application.derived_build_models import (
    DerivedBuildJob,
    DerivedBuildStatus,
    SelectedRecipeInput,
)
from ifx_registry.application.ports.derived_builds import DerivedBuildJobStore
from ifx_registry.domain.errors import (
    DerivedBuildAlreadyRunningError,
    DerivedBuildJobNotFoundError,
    OperationalStateUnavailableError,
)
from ifx_registry.domain.models import DatasetId, DatasetVersion, SnapshotKind, SnapshotRef


class SQLiteDerivedBuildJobStore(DerivedBuildJobStore):
    def __init__(self, database_path: Path):
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def add(self, job: DerivedBuildJob) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO derived_build_jobs (
                        job_id, source, dataset, output_version, recipe_revision,
                        inputs_json, status, stage, message, snapshot_id, error,
                        created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._values(job),
                )
        except sqlite3.IntegrityError as error:
            active = self.find_active(job.dataset)
            if active is not None:
                raise DerivedBuildAlreadyRunningError(
                    f"Build {active.job_id} is already active for {job.dataset}"
                ) from error
            raise

    def get(self, job_id: str) -> DerivedBuildJob:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM derived_build_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise DerivedBuildJobNotFoundError(f"Derived build job not found: {job_id}")
        return self._from_row(row)

    def save(self, job: DerivedBuildJob) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE derived_build_jobs SET
                    source = ?, dataset = ?, output_version = ?, recipe_revision = ?,
                    inputs_json = ?, status = ?, stage = ?, message = ?, snapshot_id = ?,
                    error = ?, created_at = ?, updated_at = ?, completed_at = ?
                WHERE job_id = ?
                """,
                (*self._values(job)[1:], job.job_id),
            )
        if cursor.rowcount == 0:
            raise DerivedBuildJobNotFoundError(f"Derived build job not found: {job.job_id}")

    def claim(self, job_id: str) -> DerivedBuildJob | None:
        claimed_at = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE derived_build_jobs
                SET status = ?, stage = ?, message = ?, updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    DerivedBuildStatus.RUNNING.value,
                    "materializing",
                    "Materializing exact registered inputs",
                    claimed_at,
                    job_id,
                    DerivedBuildStatus.QUEUED.value,
                ),
            )
        return self.get(job_id) if cursor.rowcount == 1 else None

    def find_active(self, dataset: DatasetId) -> DerivedBuildJob | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM derived_build_jobs
                WHERE source = ? AND dataset = ? AND status IN (?, ?)
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    dataset.source,
                    dataset.dataset,
                    DerivedBuildStatus.QUEUED.value,
                    DerivedBuildStatus.RUNNING.value,
                ),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def find_latest(self, dataset: DatasetId) -> DerivedBuildJob | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM derived_build_jobs
                WHERE source = ? AND dataset = ? AND dismissed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (dataset.source, dataset.dataset),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_active(self) -> tuple[DerivedBuildJob, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM derived_build_jobs
                WHERE status IN (?, ?) ORDER BY created_at DESC
                """,
                (DerivedBuildStatus.QUEUED.value, DerivedBuildStatus.RUNNING.value),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_attention(self) -> tuple[DerivedBuildJob, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM derived_build_jobs
                WHERE dismissed_at IS NULL
                ORDER BY created_at DESC
                """,
            ).fetchall()
        jobs = (self._from_row(row) for row in rows)
        latest_by_dataset: dict[DatasetId, DerivedBuildJob] = {}
        for job in jobs:
            latest_by_dataset.setdefault(job.dataset, job)
        return tuple(
            job
            for job in latest_by_dataset.values()
            if job.status
            in {
                DerivedBuildStatus.QUEUED,
                DerivedBuildStatus.RUNNING,
                DerivedBuildStatus.FAILED,
            }
        )

    def dismiss_failures(self) -> int:
        dismissed_at = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE derived_build_jobs SET dismissed_at = ?
                WHERE status = ? AND dismissed_at IS NULL
                """,
                (dismissed_at, DerivedBuildStatus.FAILED.value),
            )
        return cursor.rowcount

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self._database_path, timeout=30)
            connection.row_factory = sqlite3.Row
            with connection:
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
                CREATE TABLE IF NOT EXISTS derived_build_jobs (
                    job_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    output_version TEXT NOT NULL,
                    recipe_revision TEXT NOT NULL,
                    inputs_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    snapshot_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    dismissed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_derived_build_per_dataset
                ON derived_build_jobs (source, dataset)
                WHERE status IN ('queued', 'running')
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(derived_build_jobs)")
            }
            if "dismissed_at" not in columns:
                connection.execute("ALTER TABLE derived_build_jobs ADD COLUMN dismissed_at TEXT")

    def recover_interrupted(self) -> None:
        interrupted_at = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE derived_build_jobs
                SET status = ?, stage = ?, message = ?, error = ?, updated_at = ?, completed_at = ?
                WHERE status IN (?, ?)
                """,
                (
                    DerivedBuildStatus.FAILED.value,
                    "failed",
                    "Build was interrupted",
                    "The Registry service restarted before this build completed.",
                    interrupted_at,
                    interrupted_at,
                    DerivedBuildStatus.QUEUED.value,
                    DerivedBuildStatus.RUNNING.value,
                ),
            )

    @staticmethod
    def _values(job: DerivedBuildJob) -> tuple[str | None, ...]:
        return (
            job.job_id,
            job.dataset.source,
            job.dataset.dataset,
            job.output_version,
            job.recipe_revision,
            json.dumps(
                [
                    {
                        "slot": item.slot,
                        "kind": item.reference.kind.value,
                        "source": item.reference.dataset.source,
                        "dataset": item.reference.dataset.dataset,
                        "version": item.reference.version.value,
                    }
                    for item in job.inputs
                ],
                sort_keys=True,
            ),
            job.status.value,
            job.stage,
            job.message,
            job.snapshot_id,
            job.error,
            job.created_at.isoformat(),
            job.updated_at.isoformat(),
            job.completed_at.isoformat() if job.completed_at else None,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DerivedBuildJob:
        inputs = json.loads(str(row["inputs_json"]))
        return DerivedBuildJob(
            job_id=str(row["job_id"]),
            dataset=DatasetId(str(row["source"]), str(row["dataset"])),
            output_version=str(row["output_version"]),
            recipe_revision=str(row["recipe_revision"]),
            inputs=tuple(
                SelectedRecipeInput(
                    slot=str(item["slot"]),
                    reference=SnapshotRef(
                        SnapshotKind(str(item["kind"])),
                        DatasetId(str(item["source"]), str(item["dataset"])),
                        DatasetVersion(str(item["version"])),
                    ),
                )
                for item in inputs
            ),
            status=DerivedBuildStatus(str(row["status"])),
            stage=str(row["stage"]),
            message=str(row["message"]),
            snapshot_id=str(row["snapshot_id"]) if row["snapshot_id"] else None,
            error=str(row["error"]) if row["error"] else None,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            completed_at=(
                datetime.fromisoformat(str(row["completed_at"]))
                if row["completed_at"]
                else None
            ),
        )
