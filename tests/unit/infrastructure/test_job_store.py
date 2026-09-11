"""Tests for durable SQLite acquisition job state."""

from dataclasses import replace
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

    store.save(replace(job, status=AcquisitionStatus.RUNNING, stage="downloading"))

    loaded = store.get("job-1")
    assert loaded.status is AcquisitionStatus.RUNNING
    assert loaded.stage == "downloading"
    assert store.list_recent() == (loaded,)
    assert store.list_active() == (loaded,)

    store.save(replace(loaded, status=AcquisitionStatus.SUCCEEDED, stage="done"))

    assert store.list_active() == ()


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
