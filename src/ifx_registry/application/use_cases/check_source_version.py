"""Check the current upstream version of one installed source."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ifx_registry.application.contracts import DEFAULT_SOURCE_TIMEOUT, VersionProbeRequest
from ifx_registry.application.ports.catalog import SourceCatalog
from ifx_registry.application.ports.snapshots import PublishedSnapshotCatalog
from ifx_registry.application.ports.version_checks import SourceVersionCheckStore
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.domain.version_checks import SourceVersionCheck, VersionCheckOutcome


class CheckSourceVersion:
    def __init__(
        self,
        sources: SourceCatalog,
        checks: SourceVersionCheckStore | None = None,
        snapshots: PublishedSnapshotCatalog | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._sources = sources
        self._checks = checks
        self._snapshots = snapshots
        self._clock = clock

    def execute(
        self,
        dataset: DatasetId,
        *,
        timeout: timedelta = DEFAULT_SOURCE_TIMEOUT,
    ) -> SourceVersion:
        source = self._sources.get_source(dataset)
        try:
            version = source.discover_latest(VersionProbeRequest(timeout=timeout))
            version = self._registered_content_match(dataset, version)
        except Exception as error:
            if self._checks is not None:
                self._checks.append(
                    SourceVersionCheck(
                        check_id=uuid4().hex,
                        dataset=dataset,
                        checked_at=self._clock(),
                        outcome=VersionCheckOutcome.FAILED,
                        error=f"{type(error).__name__}: {error}"[:1000],
                    )
                )
            raise
        if self._checks is not None:
            self._checks.append(
                SourceVersionCheck(
                    check_id=uuid4().hex,
                    dataset=dataset,
                    checked_at=self._clock(),
                    outcome=VersionCheckOutcome.SUCCEEDED,
                    version=version,
                )
            )
        return version

    def _registered_content_match(
        self, dataset: DatasetId, version: SourceVersion
    ) -> SourceVersion:
        if self._snapshots is None:
            return version
        evidence = version.evidence
        if evidence.get("type") != "generated_content_sha256":
            return version
        digest = evidence.get("sha256")
        file_name = evidence.get("file_name")
        if not isinstance(digest, str) or not isinstance(file_name, str):
            return version
        snapshots = sorted(
            (item for item in self._snapshots.list_all() if item.dataset == dataset),
            key=lambda item: item.published_at,
            reverse=True,
        )
        for snapshot in snapshots:
            if len(snapshot.files) != 1:
                continue
            published_file = snapshot.files[0]
            if str(published_file.relative_path) == file_name and published_file.sha256 == digest:
                return snapshot.version
        return version
