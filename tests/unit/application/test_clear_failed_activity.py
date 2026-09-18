"""Tests for dismissing failed operational activity."""

from unittest.mock import create_autospec

from ifx_registry.application.ports.derived_builds import DerivedBuildJobStore
from ifx_registry.application.ports.jobs import AcquisitionJobStore
from ifx_registry.application.use_cases.clear_failed_activity import ClearFailedActivity


def test_clear_failed_activity_dismisses_both_job_types() -> None:
    acquisitions = create_autospec(AcquisitionJobStore, instance=True)
    acquisitions.dismiss_failures.return_value = 2
    derived_builds = create_autospec(DerivedBuildJobStore, instance=True)
    derived_builds.dismiss_failures.return_value = 3

    dismissed = ClearFailedActivity(acquisitions, derived_builds).execute()

    assert dismissed == 5
    acquisitions.dismiss_failures.assert_called_once_with()
    derived_builds.dismiss_failures.assert_called_once_with()
