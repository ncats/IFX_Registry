"""Publish caller-captured source data without an installed downloader."""

from ifx_registry.application.models import DatasetDescription
from ifx_registry.application.ports.snapshots import SourceSnapshotPublisher
from ifx_registry.domain.models import SourceSnapshot


class PublishSourceDataset:
    def __init__(self, publisher: SourceSnapshotPublisher):
        self._publisher = publisher

    def execute(self, snapshot: SourceSnapshot) -> DatasetDescription:
        return DatasetDescription(self._publisher.publish(snapshot))
