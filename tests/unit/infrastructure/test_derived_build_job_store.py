"""Durability and concurrency tests for derived build jobs."""

from dataclasses import replace

import pytest

from ifx_registry.application.derived_build_models import (
    DerivedBuildJob,
    DerivedBuildStatus,
    SelectedRecipeInput,
)
from ifx_registry.domain.errors import DerivedBuildAlreadyRunningError
from ifx_registry.domain.models import DatasetId, SnapshotRef
from ifx_registry.infrastructure.derived_build_job_store import SQLiteDerivedBuildJobStore


def _job(job_id: str = "job-1") -> DerivedBuildJob:
    return DerivedBuildJob(
        job_id=job_id,
        dataset=DatasetId("pubchem", "cid_molecular_info"),
        output_version="2026-09",
        recipe_revision="1",
        inputs=(
            SelectedRecipeInput(
                "compound_records",
                SnapshotRef.derived("pubchem:compound_records:2026-09"),
            ),
        ),
    )


def test_store_round_trips_exact_inputs_and_blocks_a_second_active_build(tmp_path) -> None:
    store = SQLiteDerivedBuildJobStore(tmp_path / "registry.sqlite3")
    job = _job()

    store.add(job)

    assert store.get(job.job_id) == job
    assert store.find_active(job.dataset) == job
    with pytest.raises(DerivedBuildAlreadyRunningError):
        store.add(_job("job-2"))


def test_restart_marks_interrupted_build_failed(tmp_path) -> None:
    database = tmp_path / "registry.sqlite3"
    SQLiteDerivedBuildJobStore(database).add(_job())

    restarted = SQLiteDerivedBuildJobStore(database)
    assert restarted.get("job-1").status is DerivedBuildStatus.QUEUED
    restarted.recover_interrupted()

    job = restarted.get("job-1")
    assert job.status is DerivedBuildStatus.FAILED
    assert job.completed_at is not None
    assert restarted.list_attention() == (job,)


def test_only_one_delivery_can_claim_a_queued_job(tmp_path) -> None:
    store = SQLiteDerivedBuildJobStore(tmp_path / "registry.sqlite3")
    store.add(_job())

    claimed = store.claim("job-1")

    assert claimed is not None
    assert claimed.status is DerivedBuildStatus.RUNNING
    assert store.claim("job-1") is None


def test_success_hides_an_older_failed_build_from_attention(tmp_path) -> None:
    store = SQLiteDerivedBuildJobStore(tmp_path / "registry.sqlite3")
    failed = replace(
        _job(),
        status=DerivedBuildStatus.FAILED,
        stage="failed",
        message="failed",
    )
    store.add(failed)
    succeeded = replace(
        _job("job-2"),
        status=DerivedBuildStatus.SUCCEEDED,
        stage="complete",
        message="complete",
    )
    store.add(succeeded)

    assert store.find_latest(succeeded.dataset) == succeeded
    assert store.list_attention() == ()
