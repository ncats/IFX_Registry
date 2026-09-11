"""Classify a checked upstream version against registered versions."""

from dataclasses import dataclass
from enum import StrEnum

from ifx_registry.application.use_cases.browse_catalog import PublishedDataset
from ifx_registry.domain.models import SourceVersion


class SourceUpdateState(StrEnum):
    """The safe Registry action available for one checked upstream version."""

    UP_TO_DATE = "up_to_date"
    ALREADY_REGISTERED = "already_registered"
    AVAILABLE = "available"


@dataclass(frozen=True, slots=True)
class SourceUpdateAssessment:
    checked_version: SourceVersion
    latest_registered_version: SourceVersion | None
    state: SourceUpdateState

    @property
    def can_register(self) -> bool:
        return self.state is SourceUpdateState.AVAILABLE


class AssessSourceUpdate:
    """Apply registration rules without relying on version-string ordering."""

    def execute(
        self,
        checked_version: SourceVersion,
        registered_dataset: PublishedDataset | None,
    ) -> SourceUpdateAssessment:
        if registered_dataset is None:
            state = SourceUpdateState.AVAILABLE
            latest = None
        else:
            latest = registered_dataset.latest.version
            registered_values = {snapshot.version.value for snapshot in registered_dataset.versions}
            if checked_version.value == latest.value:
                state = SourceUpdateState.UP_TO_DATE
            elif checked_version.value in registered_values:
                state = SourceUpdateState.ALREADY_REGISTERED
            else:
                state = SourceUpdateState.AVAILABLE
        return SourceUpdateAssessment(checked_version, latest, state)
