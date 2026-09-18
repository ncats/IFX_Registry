"""Ports for durable jobs and background scheduling."""

from abc import ABC, abstractmethod

from ifx_registry.domain.jobs import AcquisitionJob
from ifx_registry.domain.models import DatasetId


class AcquisitionJobStore(ABC):
    @abstractmethod
    def add(self, job: AcquisitionJob) -> None:
        """Persist a new job."""

    @abstractmethod
    def get(self, job_id: str) -> AcquisitionJob:
        """Load a job by ID."""

    @abstractmethod
    def save(self, job: AcquisitionJob) -> None:
        """Replace the stored state of an existing job."""

    @abstractmethod
    def list_recent(self, *, limit: int = 20) -> tuple[AcquisitionJob, ...]:
        """List the most recently created jobs."""

    @abstractmethod
    def list_active(self) -> tuple[AcquisitionJob, ...]:
        """List every queued or running job."""

    @abstractmethod
    def list_recent_successful(
        self,
        dataset: DatasetId,
        *,
        limit: int = 5,
    ) -> tuple[AcquisitionJob, ...]:
        """List recent successful acquisitions for one dataset."""

    @abstractmethod
    def find_active(self, dataset: DatasetId) -> AcquisitionJob | None:
        """Return the queued or running acquisition holding a dataset lock."""

    @abstractmethod
    def dismiss_failures(self) -> int:
        """Hide failed jobs from operational views while retaining their records."""


class AcquisitionScheduler(ABC):
    @abstractmethod
    def submit(self, job_id: str) -> None:
        """Schedule a persisted job for background execution."""
