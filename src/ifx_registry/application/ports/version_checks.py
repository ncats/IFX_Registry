"""Port for short-lived upstream version observations."""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping

from ifx_registry.domain.models import DatasetId
from ifx_registry.domain.version_checks import SourceCheckHistory, SourceVersionCheck


class SourceVersionCheckStore(ABC):
    @abstractmethod
    def append(self, check: SourceVersionCheck) -> None:
        """Append one immutable check attempt."""

    @abstractmethod
    def latest_for(
        self,
        datasets: Iterable[DatasetId],
    ) -> Mapping[DatasetId, SourceCheckHistory]:
        """Return the latest attempt and success for each requested dataset."""
