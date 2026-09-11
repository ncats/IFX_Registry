"""Ports for metadata-only external dataset versions."""

from abc import ABC, abstractmethod

from ifx_registry.domain.catalog import PublishedExternalDatasetVersion
from ifx_registry.domain.models import DatasetId, ExternalDatasetVersion


class ExternalDatasetVersionCatalog(ABC):
    @abstractmethod
    def list_all(self) -> tuple[PublishedExternalDatasetVersion, ...]:
        """List every committed external version assertion."""

    @abstractmethod
    def get(self, dataset: DatasetId, version: str) -> PublishedExternalDatasetVersion:
        """Load one exact external version assertion."""


class ExternalDatasetVersionPublisher(ABC):
    @abstractmethod
    def publish(self, value: ExternalDatasetVersion) -> PublishedExternalDatasetVersion:
        """Immutably commit one sanitized external version assertion."""
