"""Interpret persisted upstream checks using an explicit freshness policy."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ifx_registry.application.ports.version_checks import SourceVersionCheckStore
from ifx_registry.application.use_cases.assess_source_update import (
    AssessSourceUpdate,
    SourceUpdateAssessment,
    SourceUpdateState,
)
from ifx_registry.application.use_cases.browse_catalog import PublishedDataset
from ifx_registry.domain.models import DatasetId
from ifx_registry.domain.version_checks import SourceVersionCheck, VersionCheckOutcome


class SourceCheckState(StrEnum):
    CHECK_RECOMMENDED = "check_recommended"
    UP_TO_DATE = "up_to_date"
    UPDATE_AVAILABLE = "update_available"
    CHECK_FAILED = "check_failed"


@dataclass(frozen=True, slots=True)
class VersionCheckPolicy:
    freshness: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if self.freshness <= timedelta(0):
            raise ValueError("version-check freshness must be positive")


@dataclass(frozen=True, slots=True)
class SourceCheckStatus:
    state: SourceCheckState
    latest_attempt: SourceVersionCheck | None = None
    assessment: SourceUpdateAssessment | None = None
    fresh_until: datetime | None = None


class ListSourceCheckStatuses:
    def __init__(
        self,
        checks: SourceVersionCheckStore,
        policy: VersionCheckPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._checks = checks
        self._policy = policy or VersionCheckPolicy()
        self._clock = clock
        self._assess = AssessSourceUpdate()

    def execute(
        self,
        datasets: Iterable[DatasetId],
        registered: Mapping[DatasetId, PublishedDataset],
    ) -> Mapping[DatasetId, SourceCheckStatus]:
        requested = tuple(datasets)
        histories = self._checks.latest_for(requested)
        now = self._clock()
        result: dict[DatasetId, SourceCheckStatus] = {}
        for dataset in requested:
            history = histories.get(dataset)
            attempt = history.latest_attempt if history else None
            if attempt is None:
                result[dataset] = SourceCheckStatus(SourceCheckState.CHECK_RECOMMENDED)
                continue
            if attempt.outcome is VersionCheckOutcome.FAILED:
                result[dataset] = SourceCheckStatus(
                    SourceCheckState.CHECK_FAILED,
                    latest_attempt=attempt,
                )
                continue
            fresh_until = attempt.checked_at + self._policy.freshness
            if now >= fresh_until:
                result[dataset] = SourceCheckStatus(
                    SourceCheckState.CHECK_RECOMMENDED,
                    latest_attempt=attempt,
                    fresh_until=fresh_until,
                )
                continue
            if attempt.version is None:  # guarded by SourceVersionCheck invariants
                raise ValueError("successful version check has no version")
            assessment = self._assess.execute(attempt.version, registered.get(dataset))
            state = (
                SourceCheckState.UPDATE_AVAILABLE
                if assessment.state is SourceUpdateState.AVAILABLE
                else SourceCheckState.UP_TO_DATE
            )
            result[dataset] = SourceCheckStatus(
                state,
                latest_attempt=attempt,
                assessment=assessment,
                fresh_until=fresh_until,
            )
        return result
