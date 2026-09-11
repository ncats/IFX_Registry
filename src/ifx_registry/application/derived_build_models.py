"""Application records used while executing reusable derived recipes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ifx_registry.domain.catalog import RegisteredSnapshotRef
from ifx_registry.domain.derived_builds import RecipeInputSlot
from ifx_registry.domain.models import (
    DatasetId,
    DerivedSnapshotFile,
    SnapshotRef,
    validate_service_observations,
)


@dataclass(frozen=True, slots=True)
class SelectedRecipeInput:
    slot: str
    reference: SnapshotRef


@dataclass(frozen=True, slots=True)
class DerivedBuildPlan:
    dataset: DatasetId
    output_version: str
    recipe_revision: str
    inputs: tuple[SelectedRecipeInput, ...]
    registered_inputs: tuple[RegisteredSnapshotRef, ...]


class DerivedBuildStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED}


@dataclass(frozen=True, slots=True)
class DerivedBuildJob:
    job_id: str
    dataset: DatasetId
    output_version: str
    recipe_revision: str
    inputs: tuple[SelectedRecipeInput, ...]
    status: DerivedBuildStatus = DerivedBuildStatus.QUEUED
    stage: str = "queued"
    message: str = "Waiting to start"
    snapshot_id: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.job_id.strip() or not self.output_version.strip():
            raise ValueError("build job ID and output version must not be blank")
        if not self.inputs:
            raise ValueError("derived build job must contain selected inputs")
        if any(
            timestamp.tzinfo is None
            for timestamp in (self.created_at, self.updated_at)
        ) or (self.completed_at is not None and self.completed_at.tzinfo is None):
            raise ValueError("build job timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class MaterializedRecipeInput:
    slot: RecipeInputSlot
    reference: RegisteredSnapshotRef
    local_directory: Path | None


@dataclass(frozen=True, slots=True)
class ServiceObservation:
    """Sanitized evidence for a live service queried while producing an artifact."""

    service_id: str
    service_name: str
    interface: str
    operation: str
    endpoint_template: str
    first_observed_at: datetime
    last_observed_at: datetime
    request_count: int
    retry_count: int
    http_status_counts: Mapping[str, int]
    worst_throttle: str
    response_payload_sha256: str

    def __post_init__(self) -> None:
        validate_service_observations([self.as_metadata()])

    def as_metadata(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "service_name": self.service_name,
            "interface": self.interface,
            "operation": self.operation,
            "endpoint_template": self.endpoint_template,
            "first_observed_at": self.first_observed_at.isoformat(),
            "last_observed_at": self.last_observed_at.isoformat(),
            "request_count": self.request_count,
            "retry_count": self.retry_count,
            "http_status_counts": dict(self.http_status_counts),
            "worst_throttle": self.worst_throttle,
            "response_payload_sha256": self.response_payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class DerivedRecipeProduct:
    files: tuple[DerivedSnapshotFile, ...]
    validation: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version_date: date | None = None
    observations: tuple[ServiceObservation, ...] = ()

    def __post_init__(self) -> None:
        if not self.files:
            raise ValueError("derived recipe must produce at least one file")
