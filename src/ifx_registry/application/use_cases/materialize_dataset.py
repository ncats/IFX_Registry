"""Materialize one exact immutable source snapshot for a caller."""

from pathlib import Path

from ifx_registry.application.models import MaterializedDataset
from ifx_registry.application.ports.snapshots import (
    PublishedSnapshotCatalog,
    SnapshotMaterializationCache,
)
from ifx_registry.domain.models import DatasetId


class MaterializeDataset:
    def __init__(
        self,
        snapshots: PublishedSnapshotCatalog,
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
    ) -> MaterializedDataset:
        snapshot = self._snapshots.get(dataset, version)
        return self._cache.materialize(snapshot, Path(destination))
