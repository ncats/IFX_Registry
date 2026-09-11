"""Tests for the source publication workflow."""

from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.application.ports.jobs import AcquisitionScheduler
from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.application.use_cases.acquire_source import (
    AcquireSourceSnapshot,
    StartSourceAcquisition,
)
from ifx_registry.application.use_cases.check_source_version import CheckSourceVersion
from ifx_registry.domain.catalog import SourceDescriptor
from ifx_registry.domain.errors import (
    AcquisitionAlreadyRunningError,
    SourceVersionNotApprovedError,
)
from ifx_registry.domain.jobs import AcquisitionStatus
from ifx_registry.domain.models import DatasetId, SnapshotFile, SourceSnapshot, SourceVersion
from ifx_registry.infrastructure.catalog import InMemorySourceCatalog
from ifx_registry.infrastructure.job_store import SQLiteAcquisitionJobStore
from ifx_registry.infrastructure.local_workspaces import LocalAcquisitionWorkspaceProvider
from ifx_registry.infrastructure.s3_snapshots import S3PublishedSnapshotRepository
from ifx_registry.infrastructure.version_check_store import SQLiteSourceVersionCheckStore

from ...fakes import FakeObjectStore


class ExampleSource(SourceAdapter):
    _dataset = DatasetId("example", "records")

    @property
    def dataset(self) -> DatasetId:
        return self._dataset

    @property
    def homepage(self) -> str:
        return "https://example.org/records"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return ("https://example.org/records.tsv",)

    @property
    def version_check_description(self) -> str:
        return "Reads the example release metadata."

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return ("https://example.org/version",)

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        return SourceVersion("1")

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        local_path = request.destination / "example" / "records" / "1" / "records.tsv"
        local_path.parent.mkdir(parents=True)
        local_path.write_text("id\n1\n")
        return SourceSnapshot(
            dataset=self.dataset,
            version=SourceVersion("1"),
            files=(
                SnapshotFile(
                    local_path,
                    PurePosixPath("records.tsv"),
                    "https://example.org/records.tsv",
                ),
            ),
            downloaded_at=datetime.now(UTC),
        )


class RecordingScheduler(AcquisitionScheduler):
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    def submit(self, job_id: str) -> None:
        self.job_ids.append(job_id)


def _catalog() -> InMemorySourceCatalog:
    source = ExampleSource()
    descriptor = SourceDescriptor(source.dataset, "Example Records", "Test records", 1)
    return InMemorySourceCatalog(((descriptor, source),))


def test_acquisition_publishes_manifest_and_completes_job(tmp_path: Path) -> None:
    catalog = _catalog()
    jobs = SQLiteAcquisitionJobStore(tmp_path / "jobs.sqlite3")
    objects = FakeObjectStore()
    snapshots = S3PublishedSnapshotRepository(objects)
    scheduler = RecordingScheduler()
    checks = SQLiteSourceVersionCheckStore(tmp_path / "checks.sqlite3")
    CheckSourceVersion(catalog, checks).execute(DatasetId("example", "records"))
    start = StartSourceAcquisition(catalog, jobs, snapshots, scheduler, checks)
    job = start.execute(DatasetId("example", "records"), "1")

    AcquireSourceSnapshot(
        catalog,
        jobs,
        snapshots,
        LocalAcquisitionWorkspaceProvider(tmp_path / "work"),
    ).execute(job.job_id)

    completed = jobs.get(job.job_id)
    assert completed.status is AcquisitionStatus.SUCCEEDED
    assert completed.snapshot_id == "example:records:1"
    published = snapshots.get(DatasetId("example", "records"), "1")
    assert published.manifest_uri == ("s3://test-registry/sources/example/records/1/manifest.yaml")
    assert objects.actions[-1] == (
        "commit",
        "sources/example/records/1/manifest.yaml",
    )


def test_start_rejects_duplicate_active_job(tmp_path: Path) -> None:
    catalog = _catalog()
    jobs = SQLiteAcquisitionJobStore(tmp_path / "jobs.sqlite3")
    snapshots = S3PublishedSnapshotRepository(FakeObjectStore())
    checks = SQLiteSourceVersionCheckStore(tmp_path / "checks.sqlite3")
    CheckSourceVersion(catalog, checks).execute(DatasetId("example", "records"))
    start = StartSourceAcquisition(
        catalog,
        jobs,
        snapshots,
        RecordingScheduler(),
        checks,
    )
    start.execute(DatasetId("example", "records"), "1")

    with pytest.raises(AcquisitionAlreadyRunningError, match="already running"):
        start.execute(DatasetId("example", "records"), "1")


def test_start_rejects_version_that_was_not_checked(tmp_path: Path) -> None:
    catalog = _catalog()
    jobs = SQLiteAcquisitionJobStore(tmp_path / "jobs.sqlite3")
    checks = SQLiteSourceVersionCheckStore(tmp_path / "checks.sqlite3")
    start = StartSourceAcquisition(
        catalog,
        jobs,
        S3PublishedSnapshotRepository(FakeObjectStore()),
        RecordingScheduler(),
        checks,
    )

    with pytest.raises(SourceVersionNotApprovedError, match="Check example:records again"):
        start.execute(DatasetId("example", "records"), "invented-version")


def test_start_rejects_stale_version_check(tmp_path: Path) -> None:
    now = datetime(2026, 9, 9, tzinfo=UTC)
    catalog = _catalog()
    checks = SQLiteSourceVersionCheckStore(tmp_path / "checks.sqlite3")
    CheckSourceVersion(catalog, checks, clock=lambda: now).execute(
        DatasetId("example", "records")
    )
    start = StartSourceAcquisition(
        catalog,
        SQLiteAcquisitionJobStore(tmp_path / "jobs.sqlite3"),
        S3PublishedSnapshotRepository(FakeObjectStore()),
        RecordingScheduler(),
        checks,
        clock=lambda: now + timedelta(days=8),
    )

    with pytest.raises(SourceVersionNotApprovedError, match="Check example:records again"):
        start.execute(DatasetId("example", "records"), "1")
