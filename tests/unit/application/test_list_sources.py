"""Tests for source-list enrichment with optional operational state."""

from typing import Never

from ifx_registry.application.ports.catalog import SourceCatalog
from ifx_registry.application.ports.jobs import AcquisitionJobStore
from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.application.use_cases.list_sources import ListSources
from ifx_registry.domain.catalog import SourceDescriptor
from ifx_registry.domain.errors import OperationalStateUnavailableError
from ifx_registry.domain.jobs import AcquisitionJob
from ifx_registry.domain.models import DatasetId


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
