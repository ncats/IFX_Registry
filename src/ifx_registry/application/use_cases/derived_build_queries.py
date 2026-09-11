"""Read durable derived-build operational state through an application boundary."""

from ifx_registry.application.derived_build_models import DerivedBuildJob
from ifx_registry.application.ports.derived_builds import DerivedBuildJobStore


class DerivedBuildQueries:
    def __init__(self, jobs: DerivedBuildJobStore):
        self._jobs = jobs

    def get(self, job_id: str) -> DerivedBuildJob:
        return self._jobs.get(job_id)

    def list_attention(self) -> tuple[DerivedBuildJob, ...]:
        return self._jobs.list_attention()
