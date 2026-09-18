"""Read the authoritative published dataset catalog."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ifx_registry.application.ports.catalog import SourceCatalog
from ifx_registry.application.ports.external import ExternalDatasetVersionCatalog
from ifx_registry.application.ports.snapshots import (
    PublishedDerivedSnapshotCatalog,
    PublishedSnapshotCatalog,
)
from ifx_registry.domain.catalog import (
    PublishedDatasetSnapshot,
    PublishedDerivedSnapshot,
    PublishedExternalDatasetVersion,
    PublishedSnapshot,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.errors import RegistryError, SnapshotNotFoundError
from ifx_registry.domain.models import DatasetId, DatasetVersion, SnapshotKind, SourceVersion


class UpdateCapability(StrEnum):
    CHECK_AND_REGISTER = "check_and_register"
    CATALOG_ONLY = "catalog_only"


@dataclass(frozen=True, slots=True)
class PublishedDataset:
    dataset: DatasetId
    versions: tuple[PublishedSnapshot, ...]
    update_capability: UpdateCapability = UpdateCapability.CATALOG_ONLY

    @property
    def latest(self) -> PublishedSnapshot:
        return self.versions[0]


class BrowsePublishedCatalog:
    def __init__(
        self,
        snapshots: PublishedSnapshotCatalog,
        sources: SourceCatalog | None = None,
    ):
        self._snapshots = snapshots
        self._sources = sources

    def execute(self) -> tuple[PublishedDataset, ...]:
        grouped: dict[DatasetId, list[PublishedSnapshot]] = {}
        for snapshot in self._snapshots.list_all():
            grouped.setdefault(snapshot.dataset, []).append(snapshot)
        updatable = (
            {descriptor.dataset for descriptor in self._sources.list_descriptors()}
            if self._sources is not None
            else set()
        )
        datasets = []
        for dataset, snapshots in grouped.items():
            ordered = tuple(sorted(snapshots, key=lambda item: item.published_at, reverse=True))
            capability = (
                UpdateCapability.CHECK_AND_REGISTER
                if dataset in updatable
                else UpdateCapability.CATALOG_ONLY
            )
            datasets.append(PublishedDataset(dataset, ordered, capability))
        return tuple(sorted(datasets, key=lambda item: str(item.dataset)))


class GetPublishedSnapshot:
    def __init__(self, snapshots: PublishedSnapshotCatalog):
        self._snapshots = snapshots

    def execute(self, dataset: DatasetId, version: str) -> PublishedSnapshot:
        return self._snapshots.get(dataset, version)


class GetPublishedDataset:
    def __init__(self, catalog: BrowsePublishedCatalog):
        self._catalog = catalog

    def execute(self, dataset: DatasetId) -> PublishedDataset:
        result = next(
            (item for item in self._catalog.execute() if item.dataset == dataset),
            None,
        )
        if result is None:
            raise SnapshotNotFoundError(f"Published dataset not found: {dataset}")
        return result


class CatalogKind(StrEnum):
    SOURCE = "source"
    DERIVED = "derived"
    EXTERNAL = "external"


CatalogVersion = PublishedDatasetSnapshot | PublishedExternalDatasetVersion


class DependencyFreshness(StrEnum):
    LATEST_REGISTERED = "latest_registered"
    OLDER_VERSION = "older_version"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


class RebuildStatus(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    BROKEN = "broken"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CatalogDependency:
    reference: RegisteredSnapshotRef
    freshness: DependencyFreshness
    latest_version: DatasetVersion | SourceVersion | None

    @property
    def kind(self) -> CatalogKind:
        if self.reference.kind is SnapshotKind.SOURCE:
            return CatalogKind.SOURCE
        if self.reference.kind is SnapshotKind.DERIVED:
            return CatalogKind.DERIVED
        return CatalogKind.EXTERNAL

    @property
    def pinned_version(self) -> DatasetVersion:
        return self.reference.ref.version

    @property
    def metadata_only(self) -> bool:
        return self.reference.kind is SnapshotKind.EXTERNAL


@dataclass(frozen=True, slots=True)
class DerivedLineage:
    snapshot_id: str
    dependencies: tuple[CatalogDependency, ...]

    @property
    def latest_count(self) -> int:
        return self._count(DependencyFreshness.LATEST_REGISTERED)

    @property
    def older_count(self) -> int:
        return self._count(DependencyFreshness.OLDER_VERSION)

    @property
    def missing_count(self) -> int:
        return self._count(DependencyFreshness.MISSING)

    @property
    def unavailable_count(self) -> int:
        return self._count(DependencyFreshness.UNAVAILABLE)

    def _count(self, freshness: DependencyFreshness) -> int:
        return sum(item.freshness is freshness for item in self.dependencies)

    @property
    def status_summary(self) -> str:
        count = len(self.dependencies)
        input_word = "input" if count == 1 else "inputs"
        if count and self.latest_count == count:
            return f"All {count} {input_word} latest registered"
        parts = []
        if self.latest_count:
            parts.append(f"{self.latest_count} latest registered")
        if self.older_count:
            parts.append(f"{self.older_count} older")
        if self.missing_count:
            parts.append(f"{self.missing_count} missing")
        if self.unavailable_count:
            parts.append(f"{self.unavailable_count} status unavailable")
        return " · ".join(parts) if parts else "Dependency status unavailable"


@dataclass(frozen=True, slots=True)
class CatalogDataset:
    kind: CatalogKind
    dataset: DatasetId
    versions: tuple[CatalogVersion, ...]
    update_capability: UpdateCapability = UpdateCapability.CATALOG_ONLY
    lineages: tuple[DerivedLineage, ...] = ()
    rebuild_status: RebuildStatus | None = None

    @property
    def latest(self) -> CatalogVersion:
        return self.versions[0]

    @property
    def file_count(self) -> int | None:
        if isinstance(self.latest, PublishedExternalDatasetVersion):
            return None
        return len(self.latest.files)

    @property
    def total_size_bytes(self) -> int | None:
        if isinstance(self.latest, PublishedExternalDatasetVersion):
            return None
        return self.latest.total_size_bytes

    @property
    def latest_lineage(self) -> DerivedLineage | None:
        return self.lineage_for(self.latest.snapshot_id)

    def lineage_for(self, snapshot_id: str) -> DerivedLineage | None:
        return next(
            (lineage for lineage in self.lineages if lineage.snapshot_id == snapshot_id),
            None,
        )


class BrowseRegistryCatalog:
    """Combine kind-specific catalogs without guessing artifact kinds."""

    def __init__(
        self,
        sources: PublishedSnapshotCatalog,
        derived: PublishedDerivedSnapshotCatalog,
        external: ExternalDatasetVersionCatalog,
        installed_sources: SourceCatalog | None = None,
    ):
        self._sources = sources
        self._derived = derived
        self._external = external
        self._installed_sources = installed_sources

    def execute(self) -> tuple[CatalogDataset, ...]:
        source_versions = self._sources.list_all()
        derived_versions = self._derived.list_all()
        external_versions = self._external.list_all()
        rebuild_statuses = assess_rebuild_statuses(
            source_versions,
            derived_versions,
            external_versions,
        )
        indexes = dependency_indexes(
            source_versions,
            derived_versions,
            external_versions,
        )
        lineages = tuple(
            assess_lineage(snapshot, indexes) for snapshot in derived_versions
        )
        updatable = (
            {item.dataset for item in self._installed_sources.list_descriptors()}
            if self._installed_sources is not None
            else set()
        )
        datasets = [
            *self.group_versions(
                CatalogKind.SOURCE,
                source_versions,
                updatable=updatable,
            ),
            *self.group_versions(
                CatalogKind.DERIVED,
                derived_versions,
                lineages=lineages,
                rebuild_statuses=rebuild_statuses,
            ),
            *self.group_versions(CatalogKind.EXTERNAL, external_versions),
        ]
        return tuple(sorted(datasets, key=lambda item: (str(item.dataset), item.kind.value)))

    @staticmethod
    def group_versions(
        kind: CatalogKind,
        versions: tuple[CatalogVersion, ...],
        *,
        updatable: set[DatasetId] | None = None,
        lineages: tuple[DerivedLineage, ...] = (),
        rebuild_statuses: dict[str, RebuildStatus] | None = None,
    ) -> tuple[CatalogDataset, ...]:
        grouped: dict[DatasetId, list[CatalogVersion]] = {}
        for version in versions:
            grouped.setdefault(version.dataset, []).append(version)
        return tuple(
            CatalogDataset(
                kind=kind,
                dataset=dataset,
                versions=tuple(
                    sorted(items, key=lambda item: item.published_at, reverse=True)
                ),
                update_capability=(
                    UpdateCapability.CHECK_AND_REGISTER
                    if updatable is not None and dataset in updatable
                    else UpdateCapability.CATALOG_ONLY
                ),
                lineages=tuple(
                    lineage
                    for lineage in lineages
                    if any(item.snapshot_id == lineage.snapshot_id for item in items)
                ),
                rebuild_status=(
                    rebuild_statuses.get(
                        max(items, key=lambda item: item.published_at).snapshot_id
                    )
                    if rebuild_statuses is not None
                    else None
                ),
            )
            for dataset, items in grouped.items()
        )


class GetRegistryDataset:
    def __init__(
        self,
        sources: PublishedSnapshotCatalog,
        derived: PublishedDerivedSnapshotCatalog,
        external: ExternalDatasetVersionCatalog,
    ):
        self._sources = sources
        self._derived = derived
        self._external = external

    def execute(self, kind: CatalogKind, dataset: DatasetId) -> CatalogDataset:
        versions: tuple[CatalogVersion, ...]
        lineages: tuple[DerivedLineage, ...] = ()
        if kind is CatalogKind.SOURCE:
            versions = self._sources.list_all()
        elif kind is CatalogKind.DERIVED:
            derived_versions = self._derived.list_all()
            selected_derived = tuple(
                item for item in derived_versions if item.dataset == dataset
            )
            versions = selected_derived
            source_versions = available_versions(self._sources)
            external_versions = available_versions(self._external)
            indexes = dependency_indexes(
                source_versions,
                derived_versions,
                external_versions,
            )
            lineages = tuple(
                assess_lineage(snapshot, indexes) for snapshot in selected_derived
            )
        else:
            versions = self._external.list_all()
        result = next(
            (
                item
                for item in BrowseRegistryCatalog.group_versions(
                    kind,
                    versions,
                    lineages=lineages if kind is CatalogKind.DERIVED else (),
                )
                if item.dataset == dataset
            ),
            None,
        )
        if result is None:
            raise SnapshotNotFoundError(
                f"{kind.value.title()} dataset not found: {dataset}"
            )
        return result


CatalogVersions = tuple[CatalogVersion, ...]
CatalogReader = (
    PublishedSnapshotCatalog
    | PublishedDerivedSnapshotCatalog
    | ExternalDatasetVersionCatalog
)


@dataclass(frozen=True, slots=True)
class _DependencyIndex:
    latest_by_dataset: dict[DatasetId, CatalogVersion]
    snapshot_ids: frozenset[str]


DependencyIndexes = dict[SnapshotKind, _DependencyIndex | None]


def available_versions(catalog: CatalogReader) -> CatalogVersions | None:
    try:
        return catalog.list_all()
    except RegistryError:
        return None


def dependency_indexes(
    sources: CatalogVersions | None,
    derived: CatalogVersions | None,
    external: CatalogVersions | None,
) -> DependencyIndexes:
    return {
        SnapshotKind.SOURCE: _dependency_index(sources),
        SnapshotKind.DERIVED: _dependency_index(derived),
        SnapshotKind.EXTERNAL: _dependency_index(external),
    }


def _dependency_index(versions: CatalogVersions | None) -> _DependencyIndex | None:
    if versions is None:
        return None
    latest: dict[DatasetId, CatalogVersion] = {}
    for item in versions:
        current = latest.get(item.dataset)
        if current is None or item.published_at > current.published_at:
            latest[item.dataset] = item
    return _DependencyIndex(latest, frozenset(item.snapshot_id for item in versions))


def assess_lineage(
    snapshot: PublishedDerivedSnapshot,
    indexes: DependencyIndexes,
) -> DerivedLineage:
    dependencies = []
    for reference in snapshot.inputs:
        index = indexes[reference.kind]
        if index is None:
            dependencies.append(
                CatalogDependency(reference, DependencyFreshness.UNAVAILABLE, None)
            )
            continue
        latest = index.latest_by_dataset.get(reference.ref.dataset)
        exact_exists = reference.snapshot_id in index.snapshot_ids
        if not exact_exists:
            freshness = DependencyFreshness.MISSING
        elif (
            latest is not None
            and latest.version.value == reference.ref.version.value
        ):
            freshness = DependencyFreshness.LATEST_REGISTERED
        else:
            freshness = DependencyFreshness.OLDER_VERSION
        dependencies.append(
            CatalogDependency(
                reference,
                freshness,
                latest.version if latest is not None else None,
            )
        )
    return DerivedLineage(snapshot.snapshot_id, tuple(dependencies))


def assess_rebuild_statuses(
    sources: CatalogVersions,
    derived: tuple[PublishedDerivedSnapshot, ...],
    external: CatalogVersions,
) -> dict[str, RebuildStatus]:
    """Assess exact derived snapshots, including stale transitive inputs."""

    indexes = dependency_indexes(sources, derived, external)
    derived_by_ref = {
        (item.dataset, item.version.value): item
        for item in derived
    }
    results: dict[str, RebuildStatus] = {}
    active: set[str] = set()

    def assess(snapshot: PublishedDerivedSnapshot) -> RebuildStatus:
        if snapshot.snapshot_id in results:
            return results[snapshot.snapshot_id]
        if snapshot.snapshot_id in active:
            return RebuildStatus.UNKNOWN
        active.add(snapshot.snapshot_id)
        lineage = assess_lineage(snapshot, indexes)
        transitive = []
        for dependency in snapshot.inputs:
            if dependency.kind is not SnapshotKind.DERIVED:
                continue
            input_snapshot = derived_by_ref.get(
                (dependency.ref.dataset, dependency.ref.version.value)
            )
            if input_snapshot is not None:
                transitive.append(assess(input_snapshot))
        if lineage.unavailable_count or any(
            item is RebuildStatus.UNKNOWN for item in transitive
        ):
            status = RebuildStatus.UNKNOWN
        elif lineage.missing_count or any(
            item is RebuildStatus.BROKEN for item in transitive
        ):
            status = RebuildStatus.BROKEN
        elif lineage.older_count or any(
            item is RebuildStatus.STALE for item in transitive
        ):
            status = RebuildStatus.STALE
        else:
            status = RebuildStatus.CURRENT
        active.remove(snapshot.snapshot_id)
        results[snapshot.snapshot_id] = status
        return status

    for item in derived:
        assess(item)
    return results


class GetRegistryVersion:
    def __init__(
        self,
        sources: PublishedSnapshotCatalog,
        derived: PublishedDerivedSnapshotCatalog,
        external: ExternalDatasetVersionCatalog,
    ):
        self._sources = sources
        self._derived = derived
        self._external = external

    def execute(
        self,
        kind: CatalogKind,
        dataset: DatasetId,
        version: str,
    ) -> CatalogVersion:
        if kind is CatalogKind.SOURCE:
            return self._sources.get(dataset, version)
        if kind is CatalogKind.DERIVED:
            return self._derived.get(dataset, version)
        return self._external.get(dataset, version)
