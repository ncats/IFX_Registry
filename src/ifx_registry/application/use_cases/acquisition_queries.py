"""Read-only queries for source-acquisition operations."""

from ifx_registry.application.ports.jobs import AcquisitionJobStore
from ifx_registry.domain.jobs import AcquisitionJob


class AcquisitionQueries:
    """Read durable acquisition activity without exposing staged files."""

    def __init__(self, jobs: AcquisitionJobStore):
        self._jobs = jobs

    def get_job(self, job_id: str) -> AcquisitionJob:
        return self._jobs.get(job_id)

    def list_recent_jobs(self, *, limit: int = 20) -> tuple[AcquisitionJob, ...]:
        return self._jobs.list_recent(limit=limit)
