"""Tests for source-list enrichment with optional operational state."""

from datetime import UTC, datetime, timedelta
from typing import Never

from ifx_registry.application.ports.catalog import SourceCatalog
from ifx_registry.application.ports.jobs import AcquisitionJobStore
from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.application.use_cases.list_sources import ListSources
from ifx_registry.domain.catalog import SourceDescriptor
from ifx_registry.domain.errors import OperationalStateUnavailableError
from ifx_registry.domain.jobs import AcquisitionJob, AcquisitionStatus
from ifx_registry.domain.models import DatasetId
from ifx_registry.infrastructure.job_store import SQLiteAcquisitionJobStore


class DescriptorCatalog(SourceCatalog):
    def __init__(self, descriptor: SourceDescriptor) -> None:
        self._descriptor = descriptor

    def list_descriptors(self) -> tuple[SourceDescriptor, ...]:
        return (self._descriptor,)

    def get_source(self, dataset: DatasetId) -> SourceAdapter:
        del dataset
        raise NotImplementedError


class UnavailableJobStore(AcquisitionJobStore):
    @staticmethod
    def _unavailable() -> Never:
        raise OperationalStateUnavailableError("unavailable")

    def add(self, job: AcquisitionJob) -> None:
        del job
        self._unavailable()

    def get(self, job_id: str) -> AcquisitionJob:
        del job_id
        self._unavailable()

    def save(self, job: AcquisitionJob) -> None:
        del job
        self._unavailable()

    def list_recent(self, *, limit: int = 20) -> tuple[AcquisitionJob, ...]:
        del limit
        self._unavailable()

    def list_active(self) -> tuple[AcquisitionJob, ...]:
        self._unavailable()

    def list_recent_successful(
        self,
        dataset: DatasetId,
        *,
        limit: int = 5,
    ) -> tuple[AcquisitionJob, ...]:
        del dataset, limit
        self._unavailable()

    def find_active(self, dataset: DatasetId) -> AcquisitionJob | None:
        del dataset
        self._unavailable()


def test_catalog_sources_remain_available_when_job_state_is_unavailable() -> None:
    descriptor = SourceDescriptor(
        dataset=DatasetId("example", "records"),
        display_name="Example",
        description="Example records.",
        expected_file_count=1,
    )

    result = ListSources(DescriptorCatalog(descriptor), UnavailableJobStore()).execute()

    assert len(result) == 1
    assert result[0].descriptor == descriptor
    assert result[0].active_job is None
    assert not result[0].operational_state_available


def test_source_overview_estimates_median_request_to_completion(tmp_path) -> None:
    dataset = DatasetId("example", "records")
    descriptor = SourceDescriptor(dataset, "Example", "Example records.", 1)
    jobs = SQLiteAcquisitionJobStore(tmp_path / "registry.sqlite3")
    now = datetime.now(UTC)
    for index, minutes in enumerate((2, 8, 5), start=1):
        jobs.add(
            AcquisitionJob(
                f"job-{index}",
                dataset,
                str(index),
                status=AcquisitionStatus.SUCCEEDED,
                stage="complete",
                created_at=now + timedelta(hours=index),
                updated_at=now + timedelta(hours=index, minutes=minutes),
                completed_at=now + timedelta(hours=index, minutes=minutes),
            )
        )

    overview = ListSources(DescriptorCatalog(descriptor), jobs).execute()[0]

    assert overview.typical_request_to_completion == timedelta(minutes=5)
    assert len(overview.recent_successful_jobs) == 3
