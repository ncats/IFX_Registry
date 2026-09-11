"""Create and execute durable source-acquisition jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ifx_registry.application.contracts import FetchRequest
from ifx_registry.application.ports.catalog import SourceCatalog
from ifx_registry.application.ports.jobs import AcquisitionJobStore, AcquisitionScheduler
from ifx_registry.application.ports.snapshots import (
    AcquisitionWorkspaceProvider,
    PublishedSnapshotCatalog,
    SourceSnapshotPublisher,
)
from ifx_registry.application.ports.version_checks import SourceVersionCheckStore
from ifx_registry.application.progress import ProgressReporter, ProgressUpdate
from ifx_registry.application.use_cases.fetch_source import FetchSource
from ifx_registry.domain.errors import (
    AcquisitionAlreadyRunningError,
    RegistryError,
    SnapshotAlreadyExistsError,
    SnapshotNotFoundError,
    SourceVersionNotApprovedError,
)
from ifx_registry.domain.jobs import AcquisitionJob, AcquisitionStatus
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.domain.version_checks import VersionCheckOutcome


class StartSourceAcquisition:
    """Validate and enqueue one human-approved source version."""

    def __init__(
        self,
        sources: SourceCatalog,
        jobs: AcquisitionJobStore,
        snapshots: PublishedSnapshotCatalog,
        scheduler: AcquisitionScheduler,
        checks: SourceVersionCheckStore,
        *,
        check_freshness: timedelta = timedelta(days=7),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if check_freshness <= timedelta(0):
            raise ValueError("check_freshness must be positive")
        self._sources = sources
        self._jobs = jobs
        self._snapshots = snapshots
        self._scheduler = scheduler
        self._checks = checks
        self._check_freshness = check_freshness
        self._clock = clock

    def execute(self, dataset: DatasetId, expected_version: str) -> AcquisitionJob:
        self._sources.get_source(dataset)
        normalized_version = SourceVersion(expected_version).value
        self._require_recent_check(dataset, normalized_version)
        try:
            self._snapshots.get(dataset, normalized_version)
        except SnapshotNotFoundError:
            pass
        else:
            raise SnapshotAlreadyExistsError(
                f"Version is already published: {dataset}:{normalized_version}"
            )
        active_job = self._jobs.find_active(dataset)
        if active_job is not None:
            raise AcquisitionAlreadyRunningError(
                f"Acquisition {active_job.job_id} is already running for "
                f"{dataset}:{normalized_version}"
            )
        job = AcquisitionJob(
            job_id=uuid4().hex,
            dataset=dataset,
            expected_version=normalized_version,
        )
        self._jobs.add(job)
        try:
            self._scheduler.submit(job.job_id)
        except Exception as error:
            failed_at = datetime.now(UTC)
            failed = replace(
                job,
                status=AcquisitionStatus.FAILED,
                stage="failed",
                message="Could not schedule acquisition",
                error=str(error),
                updated_at=failed_at,
                completed_at=failed_at,
            )
            self._jobs.save(failed)
            raise
        return job

    def _require_recent_check(self, dataset: DatasetId, expected_version: str) -> None:
        history = self._checks.latest_for((dataset,)).get(dataset)
        attempt = history.latest_attempt if history else None
        if (
            attempt is None
            or attempt.outcome is not VersionCheckOutcome.SUCCEEDED
            or attempt.version is None
            or attempt.version.value != expected_version
            or self._clock() >= attempt.checked_at + self._check_freshness
        ):
            raise SourceVersionNotApprovedError(
                f"Check {dataset} again before downloading and registering "
                f"version {expected_version}"
            )


class _JobProgressReporter(ProgressReporter):
    def __init__(self, jobs: AcquisitionJobStore, job_id: str):
        self._jobs = jobs
        self._job_id = job_id

    def report(self, update: ProgressUpdate) -> None:
        job = self._jobs.get(self._job_id)
        message = update.message
        if update.completed is not None and update.total is not None:
            message = f"{message} ({update.completed}/{update.total})"
        self._jobs.save(
            replace(
                job,
                stage=update.stage,
                message=message,
                updated_at=datetime.now(UTC),
            )
        )


class AcquireSourceSnapshot:
    """Fetch, validate, and publish one queued source release."""

    def __init__(
        self,
        sources: SourceCatalog,
        jobs: AcquisitionJobStore,
        snapshots: SourceSnapshotPublisher,
        workspaces: AcquisitionWorkspaceProvider,
    ):
        self._sources = sources
        self._jobs = jobs
        self._snapshots = snapshots
        self._workspaces = workspaces

    def execute(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        started_at = datetime.now(UTC)
        self._jobs.save(
            replace(
                job,
                status=AcquisitionStatus.RUNNING,
                stage="checking",
                message="Checking the approved upstream version",
                updated_at=started_at,
            )
        )

        try:
            source = self._sources.get_source(job.dataset)
            reporter = _JobProgressReporter(self._jobs, job_id)
            with self._workspaces.open(job_id) as destination:
                snapshot = FetchSource().execute(
                    source,
                    FetchRequest(
                        destination=destination,
                        expected_version=SourceVersion(job.expected_version),
                        progress=reporter,
                    ),
                )
                reporter.report(
                    ProgressUpdate(
                        stage="publishing",
                        message="Calculating checksums and registering the files",
                    )
                )
                published = self._snapshots.publish(snapshot)

            completed_at = datetime.now(UTC)
            current = self._jobs.get(job_id)
            self._jobs.save(
                replace(
                    current,
                    status=AcquisitionStatus.SUCCEEDED,
                    stage="complete",
                    message="Downloaded, validated, and registered in the Registry",
                    snapshot_id=published.snapshot_id,
                    updated_at=completed_at,
                    completed_at=completed_at,
                )
            )
        except Exception as error:
            failed_at = datetime.now(UTC)
            current = self._jobs.get(job_id)
            if isinstance(error, RegistryError):
                error_message = str(error)
            else:
                error_message = f"{type(error).__name__}: {error}"
            self._jobs.save(
                replace(
                    current,
                    status=AcquisitionStatus.FAILED,
                    stage="failed",
                    message="Acquisition failed",
                    error=error_message,
                    updated_at=failed_at,
                    completed_at=failed_at,
                )
            )
