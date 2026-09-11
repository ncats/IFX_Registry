"""Materialize one named file from an exact immutable source snapshot."""

from pathlib import Path

from ifx_registry.application.ports.snapshots import (
    PublishedSnapshotCatalog,
    SnapshotMaterializationCache,
)
from ifx_registry.domain.models import DatasetId

from ._file_selection import select_file


class MaterializeFile:
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
        file_name: str | None,
        *,
        destination: Path,
    ) -> Path:
        snapshot = self._snapshots.get(dataset, version)
        selected = select_file(snapshot, file_name)
        return self._cache.materialize_file(snapshot, selected, Path(destination))
