"""Build the small source overview needed by presentation layers."""

from dataclasses import dataclass

from ifx_registry.application.ports.catalog import SourceCatalog
from ifx_registry.application.ports.jobs import AcquisitionJobStore
from ifx_registry.domain.catalog import SourceDescriptor
from ifx_registry.domain.errors import OperationalStateUnavailableError
from ifx_registry.domain.jobs import AcquisitionJob


@dataclass(frozen=True, slots=True)
class SourceOverview:
    descriptor: SourceDescriptor
    latest_job: AcquisitionJob | None
    active_job: AcquisitionJob | None
    operational_state_available: bool = True


class ListSources:
    def __init__(
        self,
        sources: SourceCatalog,
        jobs: AcquisitionJobStore,
    ):
        self._sources = sources
        self._jobs = jobs

    def execute(self) -> tuple[SourceOverview, ...]:
        try:
            recent_jobs = self._jobs.list_recent(limit=100)
            active_jobs = self._jobs.list_active()
        except OperationalStateUnavailableError:
            return tuple(
                SourceOverview(
                    descriptor=descriptor,
                    latest_job=None,
                    active_job=None,
                    operational_state_available=False,
                )
                for descriptor in self._sources.list_descriptors()
            )
        latest_jobs: dict[str, AcquisitionJob] = {}
        for job in recent_jobs:
            latest_jobs.setdefault(str(job.dataset), job)
        active_by_dataset = {str(job.dataset): job for job in active_jobs}
        return tuple(
            SourceOverview(
                descriptor=descriptor,
                latest_job=latest_jobs.get(str(descriptor.dataset)),
                active_job=active_by_dataset.get(str(descriptor.dataset)),
            )
            for descriptor in self._sources.list_descriptors()
        )
