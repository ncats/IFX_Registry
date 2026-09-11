"""Persistable observations from upstream version checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ifx_registry.domain.models import DatasetId, SourceVersion


class VersionCheckOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SourceVersionCheck:
    check_id: str
    dataset: DatasetId
    checked_at: datetime
    outcome: VersionCheckOutcome
    version: SourceVersion | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.check_id.strip():
            raise ValueError("version check ID must not be blank")
        if self.checked_at.tzinfo is None:
            raise ValueError("version check timestamp must be timezone-aware")
        if self.outcome is VersionCheckOutcome.SUCCEEDED:
            if self.version is None or self.error is not None:
                raise ValueError("a successful check requires a version and no error")
        elif self.version is not None or not self.error:
            raise ValueError("a failed check requires an error and no version")


@dataclass(frozen=True, slots=True)
class SourceCheckHistory:
    latest_attempt: SourceVersionCheck | None
    latest_success: SourceVersionCheck | None
