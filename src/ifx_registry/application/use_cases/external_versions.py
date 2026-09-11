"""Register and query metadata-only external dataset versions."""

from ifx_registry.application.models import ExternalDatasetDescription
from ifx_registry.application.ports.external import (
    ExternalDatasetVersionCatalog,
    ExternalDatasetVersionPublisher,
)
from ifx_registry.domain.models import DatasetId, ExternalDatasetVersion


class RegisterExternalDatasetVersion:
    def __init__(self, publisher: ExternalDatasetVersionPublisher):
        self._publisher = publisher

    def execute(self, value: ExternalDatasetVersion) -> ExternalDatasetDescription:
        return ExternalDatasetDescription(self._publisher.publish(value))


class DescribeExternalDatasetVersion:
    def __init__(self, catalog: ExternalDatasetVersionCatalog):
        self._catalog = catalog

    def execute(self, dataset: DatasetId, version: str) -> ExternalDatasetDescription:
        return ExternalDatasetDescription(self._catalog.get(dataset, version))


class ListExternalDatasetVersions:
    def __init__(self, catalog: ExternalDatasetVersionCatalog):
        self._catalog = catalog

    def execute(
        self,
        *,
        source: str | None = None,
        dataset: str | None = None,
    ) -> tuple[ExternalDatasetDescription, ...]:
        if dataset is not None and source is None:
            raise ValueError("source is required when filtering external versions by dataset")
        return tuple(
            ExternalDatasetDescription(item)
            for item in self._catalog.list_all()
            if (source is None or item.dataset.source == source)
            and (dataset is None or item.dataset.dataset == dataset)
        )
