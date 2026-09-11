"""Ports for published snapshots and temporary acquisition workspaces."""

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from pathlib import Path
from typing import overload

from ifx_registry.application.models import DerivedMaterializedDataset, MaterializedDataset
from ifx_registry.domain.catalog import (
    PublishedDatasetSnapshot,
    PublishedDerivedSnapshot,
    PublishedFile,
    PublishedSnapshot,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.models import DatasetId, DerivedSnapshot, SourceSnapshot


class PublishedSnapshotCatalog(ABC):
    @abstractmethod
    def list_all(self) -> tuple[PublishedSnapshot, ...]:
        """List every snapshot committed to authoritative storage."""

    @abstractmethod
    def get(self, dataset: DatasetId, version: str) -> PublishedSnapshot:
        """Load one exact published snapshot."""


class SourceSnapshotPublisher(ABC):
    @abstractmethod
    def publish(self, snapshot: SourceSnapshot) -> PublishedSnapshot:
        """Commit a validated source snapshot to authoritative storage."""


class PublishedDerivedSnapshotCatalog(ABC):
    @abstractmethod
    def list_all(self) -> tuple[PublishedDerivedSnapshot, ...]:
        """List every derived snapshot committed to authoritative storage."""

    @abstractmethod
    def get(self, dataset: DatasetId, version: str) -> PublishedDerivedSnapshot:
        """Load one exact derived snapshot."""


class DerivedSnapshotPublisher(ABC):
    @abstractmethod
    def publish(
        self,
        snapshot: DerivedSnapshot,
        inputs: tuple[RegisteredSnapshotRef, ...],
    ) -> PublishedDerivedSnapshot:
        """Commit derived data after all exact inputs have been verified."""


class PublishedFileReader(ABC):
    @abstractmethod
    def download(self, file: PublishedFile, destination: Path) -> None:
        """Stream one declared published file to a caller-provided path."""


class SnapshotMaterializationCache(ABC):
    @overload
    def materialize(
        self,
        snapshot: PublishedSnapshot,
        destination: Path,
    ) -> MaterializedDataset: ...

    @overload
    def materialize(
        self,
        snapshot: PublishedDerivedSnapshot,
        destination: Path,
    ) -> DerivedMaterializedDataset: ...

    @abstractmethod
    def materialize(
        self,
        snapshot: PublishedDatasetSnapshot,
        destination: Path,
    ) -> MaterializedDataset | DerivedMaterializedDataset:
        """Verify one snapshot into a consumer-owned cache."""

    @abstractmethod
    def materialize_file(
        self,
        snapshot: PublishedDatasetSnapshot,
        file: PublishedFile,
        destination: Path,
    ) -> Path:
        """Verify one selected snapshot file into a consumer-owned cache."""


class JobWorkspaceProvider(ABC):
    @abstractmethod
    def open(self, job_id: str) -> AbstractContextManager[Path]:
        """Create a temporary working root that is removed when the job finishes."""


class AcquisitionWorkspaceProvider(JobWorkspaceProvider):
    """Compatibility name for source-acquisition workspace providers."""
