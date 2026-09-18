"""Dismiss failed operational jobs from presentation-layer activity views."""

from ifx_registry.application.ports.derived_builds import DerivedBuildJobStore
from ifx_registry.application.ports.jobs import AcquisitionJobStore


class ClearFailedActivity:
    def __init__(
        self,
        acquisitions: AcquisitionJobStore,
        derived_builds: DerivedBuildJobStore,
    ) -> None:
        self._acquisitions = acquisitions
        self._derived_builds = derived_builds

    def execute(self) -> int:
        """Dismiss all failures that have completed before this request."""
        return self._acquisitions.dismiss_failures() + self._derived_builds.dismiss_failures()
