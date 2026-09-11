"""Build the small source overview needed by presentation layers."""

from dataclasses import dataclass
from datetime import timedelta
from statistics import median

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
    recent_successful_jobs: tuple[AcquisitionJob, ...] = ()
    operational_state_available: bool = True

    @property
    def typical_request_to_completion(self) -> timedelta | None:
        durations = [
            duration.total_seconds()
            for job in self.recent_successful_jobs
            if (duration := job.request_to_completion) is not None
        ]
        if not durations:
            return None
        return timedelta(seconds=median(durations))


class ListSources:
    def __init__(
        self,
        sources: SourceCatalog,
        jobs: AcquisitionJobStore,
    ):
        self._sources = sources
        self._jobs = jobs

    def execute(self) -> tuple[SourceOverview, ...]:
        descriptors = self._sources.list_descriptors()
        try:
            recent_jobs = self._jobs.list_recent(limit=100)
            active_jobs = self._jobs.list_active()
            successful_by_dataset = {
                descriptor.dataset: self._jobs.list_recent_successful(
                    descriptor.dataset,
                    limit=5,
                )
                for descriptor in descriptors
            }
        except OperationalStateUnavailableError:
            return tuple(
                SourceOverview(
                    descriptor=descriptor,
                    latest_job=None,
                    active_job=None,
                    operational_state_available=False,
                )
                for descriptor in descriptors
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
                recent_successful_jobs=successful_by_dataset[descriptor.dataset],
            )
            for descriptor in descriptors
        )
