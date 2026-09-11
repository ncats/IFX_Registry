"""Publish and consume exact caller-produced dataset snapshots."""

from pathlib import Path

from ifx_registry.application.models import (
    DerivedDatasetDescription,
    DerivedMaterializedDataset,
)
from ifx_registry.application.ports.external import ExternalDatasetVersionCatalog
from ifx_registry.application.ports.snapshots import (
    DerivedSnapshotPublisher,
    PublishedDerivedSnapshotCatalog,
    PublishedSnapshotCatalog,
    SnapshotMaterializationCache,
)
from ifx_registry.domain.catalog import (
    PublishedDatasetSnapshot,
    PublishedExternalDatasetVersion,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.errors import InvalidPublicationError
from ifx_registry.domain.models import DatasetId, DerivedSnapshot, SnapshotKind

from ._file_selection import select_file


class PublishDerivedDataset:
    def __init__(
        self,
        sources: PublishedSnapshotCatalog,
        derived: PublishedDerivedSnapshotCatalog,
        external: ExternalDatasetVersionCatalog,
        publisher: DerivedSnapshotPublisher,
    ):
        self._sources = sources
        self._derived = derived
        self._external = external
        self._publisher = publisher

    def execute(self, snapshot: DerivedSnapshot) -> DerivedDatasetDescription:
        resolved = []
        for dependency in snapshot.inputs:
            published: PublishedDatasetSnapshot | PublishedExternalDatasetVersion
            if dependency.kind is SnapshotKind.SOURCE:
                published = self._sources.get(dependency.dataset, dependency.version.value)
            elif dependency.kind is SnapshotKind.DERIVED:
                published = self._derived.get(dependency.dataset, dependency.version.value)
            elif dependency.kind is SnapshotKind.EXTERNAL:
                published = self._external.get(dependency.dataset, dependency.version.value)
            else:  # pragma: no cover - enum construction prevents this
                raise InvalidPublicationError(
                    f"Unsupported derived input kind: {dependency.kind}"
                )
            resolved.append(
                RegisteredSnapshotRef(
                    ref=dependency,
                    manifest_uri=published.manifest_uri,
                    manifest_sha256=published.manifest_sha256,
                )
            )
        return DerivedDatasetDescription(self._publisher.publish(snapshot, tuple(resolved)))


class DescribeDerivedDataset:
    def __init__(self, snapshots: PublishedDerivedSnapshotCatalog):
        self._snapshots = snapshots

    def execute(self, dataset: DatasetId, version: str) -> DerivedDatasetDescription:
        return DerivedDatasetDescription(self._snapshots.get(dataset, version))


class MaterializeDerivedDataset:
    def __init__(
        self,
        snapshots: PublishedDerivedSnapshotCatalog,
        cache: SnapshotMaterializationCache,
    ):
        self._snapshots = snapshots
        self._cache = cache

    def execute(
        self,
        dataset: DatasetId,
        version: str,
        *,
        destination: Path,
    ) -> DerivedMaterializedDataset:
        return self._cache.materialize(
            self._snapshots.get(dataset, version),
            Path(destination),
        )


class MaterializeDerivedFile:
    def __init__(
        self,
        snapshots: PublishedDerivedSnapshotCatalog,
        cache: SnapshotMaterializationCache,
    ):
        self._snapshots = snapshots
        self._cache = cache

    def execute(
        self,
        dataset: DatasetId,
        version: str,
        file_name: str | None,
        *,
        destination: Path,
    ) -> Path:
        snapshot = self._snapshots.get(dataset, version)
        return self._cache.materialize_file(
            snapshot,
            select_file(snapshot, file_name),
            Path(destination),
        )
