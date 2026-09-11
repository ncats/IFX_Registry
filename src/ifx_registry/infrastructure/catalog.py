"""In-memory composition of installed source adapters."""

from collections.abc import Iterable

from ifx_registry.application.ports.catalog import SourceCatalog
from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.domain.catalog import SourceDescriptor
from ifx_registry.domain.errors import UnknownSourceError
from ifx_registry.domain.models import DatasetId


class InMemorySourceCatalog(SourceCatalog):
    def __init__(
        self,
        entries: Iterable[tuple[SourceDescriptor, SourceAdapter]],
    ):
        self._descriptors: list[SourceDescriptor] = []
        self._sources: dict[DatasetId, SourceAdapter] = {}
        for descriptor, source in entries:
            if descriptor.dataset != source.dataset:
                raise ValueError(
                    f"Source descriptor {descriptor.dataset} does not match "
                    f"adapter {source.dataset}"
                )
            if descriptor.dataset in self._sources:
                raise ValueError(f"Duplicate source adapter for {descriptor.dataset}")
            self._descriptors.append(descriptor)
            self._sources[descriptor.dataset] = source

    def list_descriptors(self) -> tuple[SourceDescriptor, ...]:
        return tuple(self._descriptors)

    def get_source(self, dataset: DatasetId) -> SourceAdapter:
        try:
            return self._sources[dataset]
        except KeyError as error:
            raise UnknownSourceError(f"No source adapter is installed for {dataset}") from error
