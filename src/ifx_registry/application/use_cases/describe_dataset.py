"""Inspect one exact registered snapshot without downloading its files."""

from ifx_registry.application.models import DatasetDescription
from ifx_registry.application.ports.snapshots import PublishedSnapshotCatalog
from ifx_registry.domain.models import DatasetId


class DescribeDataset:
    def __init__(self, snapshots: PublishedSnapshotCatalog):
        self._snapshots = snapshots

    def execute(self, dataset: DatasetId, version: str) -> DatasetDescription:
        return DatasetDescription(self._snapshots.get(dataset, version))
