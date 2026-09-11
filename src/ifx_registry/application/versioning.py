"""Cross-source version policy."""

from ifx_registry.domain.errors import VersionMismatchError
from ifx_registry.domain.models import DatasetId, SourceVersion


def ensure_expected_version(
    dataset: DatasetId,
    actual: SourceVersion,
    expected: SourceVersion | None,
) -> None:
    """Fail before acquisition when a mutable upstream has moved past a pinned version."""
    if expected is not None and actual.value != expected.value:
        raise VersionMismatchError(
            f"Expected {dataset} version {expected}, but upstream currently reports {actual}"
        )
