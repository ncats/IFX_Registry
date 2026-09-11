"""Domain model for durable source-acquisition jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from ifx_registry.domain.models import DatasetId


class AcquisitionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED}


@dataclass(frozen=True, slots=True)
class AcquisitionJob:
    """Persistent state of one requested source acquisition."""

    job_id: str
    dataset: DatasetId
    expected_version: str
    status: AcquisitionStatus = AcquisitionStatus.QUEUED
    stage: str = "queued"
    message: str = "Waiting to start"
    snapshot_id: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job_id must not be blank")
        if not self.expected_version.strip():
            raise ValueError("expected_version must not be blank")
        timestamps = [self.created_at, self.updated_at]
        if self.completed_at is not None:
            timestamps.append(self.completed_at)
        if any(timestamp.tzinfo is None for timestamp in timestamps):
            raise ValueError("job timestamps must be timezone-aware")
