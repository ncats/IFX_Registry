"""Tests for durable SQLite acquisition job state."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ifx_registry.domain.errors import AcquisitionAlreadyRunningError
from ifx_registry.domain.jobs import AcquisitionJob, AcquisitionStatus
from ifx_registry.domain.models import DatasetId
from ifx_registry.infrastructure.job_store import SQLiteAcquisitionJobStore


def test_job_round_trips_and_updates(tmp_path: Path) -> None:
    store = SQLiteAcquisitionJobStore(tmp_path / "jobs.sqlite3")
    job = AcquisitionJob("job-1", DatasetId("example", "records"), "1")
    store.add(job)

    started_at = datetime.now(UTC)
    store.save(
        replace(
            job,
            status=AcquisitionStatus.RUNNING,
            stage="downloading",
            started_at=started_at,
        )
    )

    loaded = store.get("job-1")
    assert loaded.status is AcquisitionStatus.RUNNING
    assert loaded.stage == "downloading"
    assert loaded.started_at == started_at
    assert store.list_recent() == (loaded,)
    assert store.list_active() == (loaded,)

    store.save(replace(loaded, status=AcquisitionStatus.SUCCEEDED, stage="done"))

    assert store.list_active() == ()


def test_lists_recent_successful_jobs_for_one_dataset(tmp_path: Path) -> None:
    store = SQLiteAcquisitionJobStore(tmp_path / "jobs.sqlite3")
    dataset = DatasetId("example", "records")
    other = DatasetId("other", "records")
    now = datetime.now(UTC)
    for index, selected_dataset in enumerate((dataset, other, dataset), start=1):
        created_at = now + timedelta(minutes=index)
        job = AcquisitionJob(
            f"job-{index}",
            selected_dataset,
            str(index),
            status=AcquisitionStatus.SUCCEEDED,
            stage="complete",
            created_at=created_at,
            started_at=created_at,
            updated_at=created_at + timedelta(minutes=index),
            completed_at=created_at + timedelta(minutes=index),
        )
        store.add(job)

    successful = store.list_recent_successful(dataset, limit=1)

    assert tuple(job.job_id for job in successful) == ("job-3",)


def test_existing_database_adds_nullable_started_at_column(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE acquisition_jobs (
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
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )

    store = SQLiteAcquisitionJobStore(database)
    job = AcquisitionJob("job-1", DatasetId("example", "records"), "1")
    store.add(job)

    assert store.get(job.job_id).started_at is None


def test_restart_marks_unfinished_job_as_failed(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    first_store = SQLiteAcquisitionJobStore(database_path)
    first_store.add(AcquisitionJob("job-1", DatasetId("example", "records"), "1"))

    restarted_store = SQLiteAcquisitionJobStore(database_path)

    recovered = restarted_store.get("job-1")
    assert recovered.status is AcquisitionStatus.FAILED
    assert "restarted" in (recovered.error or "")


def test_database_enforces_one_active_job_per_dataset(tmp_path: Path) -> None:
    store = SQLiteAcquisitionJobStore(tmp_path / "jobs.sqlite3")
    dataset = DatasetId("example", "records")
    first = AcquisitionJob("job-1", dataset, "1")
    store.add(first)

    with pytest.raises(AcquisitionAlreadyRunningError, match="already active"):
        store.add(AcquisitionJob("job-2", dataset, "2"))

    assert store.find_active(dataset) == first
