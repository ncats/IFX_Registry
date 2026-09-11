"""Port for the set of source adapters installed in an application."""

from abc import ABC, abstractmethod

from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.domain.catalog import SourceDescriptor
from ifx_registry.domain.models import DatasetId


class SourceCatalog(ABC):
    @abstractmethod
    def list_descriptors(self) -> tuple[SourceDescriptor, ...]:
        """List installed sources in presentation order."""

    @abstractmethod
    def get_source(self, dataset: DatasetId) -> SourceAdapter:
        """Return the adapter for one installed source."""
