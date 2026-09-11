"""Build an exact, bidirectional lineage graph for Registry detail pages."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass

from ifx_registry.application.ports.derived_builds import DerivedRecipeCatalog
from ifx_registry.application.ports.external import ExternalDatasetVersionCatalog
from ifx_registry.application.ports.snapshots import (
    PublishedDerivedSnapshotCatalog,
    PublishedSnapshotCatalog,
)
from ifx_registry.application.use_cases.browse_catalog import (
    BrowseRegistryCatalog,
    CatalogDataset,
    CatalogKind,
    CatalogVersion,
    DependencyFreshness,
    assess_lineage,
    assess_rebuild_statuses,
    dependency_indexes,
)
from ifx_registry.domain.catalog import PublishedDerivedSnapshot
from ifx_registry.domain.errors import SnapshotNotFoundError
from ifx_registry.domain.models import DatasetId, SnapshotKind


@dataclass(frozen=True, slots=True)
class LineageSnapshotKey:
    """Kind-qualified identity for one exact registered snapshot."""

    kind: CatalogKind
    dataset: DatasetId
    version: str

    @property
    def snapshot_id(self) -> str:
        return f"{self.dataset}:{self.version}"


@dataclass(frozen=True, slots=True)
class LineageNodeReference:
    key: LineageSnapshotKey
    exists: bool


@dataclass(frozen=True, slots=True)
class LineageNode:
    key: LineageSnapshotKey
    snapshot: CatalogVersion | None
    freshness: DependencyFreshness
    latest_version: str | None
    dependencies: tuple[LineageNodeReference, ...]
    used_by: tuple[LineageNodeReference, ...]
    level: int

    @property
    def is_missing(self) -> bool:
        return self.snapshot is None

    @property
    def metadata_only(self) -> bool:
        return self.key.kind is CatalogKind.EXTERNAL

    @property
    def service_observations(self) -> tuple[Mapping[str, object], ...]:
        if not isinstance(self.snapshot, PublishedDerivedSnapshot):
            return ()
        value = self.snapshot.metadata.get("service_observations")
        if not isinstance(value, list):
            return ()
        return tuple(
            item
            for item in value
            if isinstance(item, dict)
            and isinstance(item.get("service_id"), str)
            and isinstance(item.get("service_name"), str)
        )


@dataclass(frozen=True, slots=True)
class LineageDatasetGroup:
    kind: CatalogKind
    dataset: DatasetId
    nodes: tuple[LineageNode, ...]
    overview_available: bool


@dataclass(frozen=True, slots=True)
class DatasetLineage:
    """The complete connected exact-snapshot graph containing the focus."""

    focus: LineageSnapshotKey
    nodes: tuple[LineageNode, ...]
    has_cycle: bool = False
    buildable_derived_datasets: frozenset[DatasetId] = frozenset()

    @property
    def levels(self) -> tuple[tuple[LineageNode, ...], ...]:
        grouped: dict[int, list[LineageNode]] = defaultdict(list)
        for node in self.nodes:
            grouped[node.level].append(node)
        return tuple(
            tuple(sorted(grouped[level], key=lambda item: _key_sort(item.key)))
            for level in sorted(grouped)
        )

    @property
    def grouped_levels(self) -> tuple[tuple[LineageDatasetGroup, ...], ...]:
        result = []
        for level in self.levels:
            grouped: dict[tuple[CatalogKind, DatasetId], list[LineageNode]] = defaultdict(list)
            for node in level:
                grouped[(node.key.kind, node.key.dataset)].append(node)
            result.append(
                tuple(
                    LineageDatasetGroup(
                        kind,
                        dataset,
                        tuple(nodes),
                        overview_available=(
                            any(
                                not node.is_missing or node.latest_version is not None
                                for node in nodes
                            )
                            or (
                                kind is CatalogKind.DERIVED
                                and dataset in self.buildable_derived_datasets
                            )
                        ),
                    )
                    for (kind, dataset), nodes in sorted(
                        grouped.items(),
                        key=lambda item: (item[0][0].value, str(item[0][1])),
                    )
                )
            )
        return tuple(result)


@dataclass(frozen=True, slots=True)
class RegistryDatasetDetails:
    dataset: CatalogDataset
    selected: CatalogVersion
    lineage: DatasetLineage


class GetRegistryDatasetDetails:
    """Read all catalogs once and project one dataset plus its full lineage."""

    def __init__(
        self,
        sources: PublishedSnapshotCatalog,
        derived: PublishedDerivedSnapshotCatalog,
        external: ExternalDatasetVersionCatalog,
        recipes: DerivedRecipeCatalog,
    ):
        self._sources = sources
        self._derived = derived
        self._external = external
        self._recipes = recipes

    def execute(
        self,
        kind: CatalogKind,
        dataset: DatasetId,
        version: str | None = None,
    ) -> RegistryDatasetDetails:
        source_versions = self._sources.list_all()
        derived_versions = self._derived.list_all()
        external_versions = self._external.list_all()
        versions_by_kind: dict[CatalogKind, tuple[CatalogVersion, ...]] = {
            CatalogKind.SOURCE: source_versions,
            CatalogKind.DERIVED: derived_versions,
            CatalogKind.EXTERNAL: external_versions,
        }
        selected_versions = versions_by_kind[kind]
        indexes = dependency_indexes(source_versions, derived_versions, external_versions)
        rebuild_statuses = assess_rebuild_statuses(
            source_versions,
            derived_versions,
            external_versions,
        )
        lineages = (
            tuple(
                assess_lineage(snapshot, indexes)
                for snapshot in derived_versions
                if snapshot.dataset == dataset
            )
            if kind is CatalogKind.DERIVED
            else ()
        )
        catalog_dataset = next(
            (
                item
                for item in BrowseRegistryCatalog.group_versions(
                    kind,
                    selected_versions,
                    lineages=lineages,
                    rebuild_statuses=(
                        rebuild_statuses if kind is CatalogKind.DERIVED else None
                    ),
                )
                if item.dataset == dataset
            ),
            None,
        )
        if catalog_dataset is None:
            raise SnapshotNotFoundError(
                f"{kind.value.title()} dataset not found: {dataset}"
            )
        selected = (
            catalog_dataset.latest
            if version is None
            else next(
                (
                    item
                    for item in catalog_dataset.versions
                    if item.version.value == version
                ),
                None,
            )
        )
        if selected is None:
            raise SnapshotNotFoundError(
                f"{kind.value.title()} snapshot not found: {dataset}:{version}"
            )
        lineage = _build_lineage(
            focus=_key(kind, selected),
            versions_by_kind=versions_by_kind,
            derived_versions=derived_versions,
            buildable_derived_datasets=frozenset(
                descriptor.dataset for descriptor in self._recipes.list_descriptors()
            ),
        )
        return RegistryDatasetDetails(catalog_dataset, selected, lineage)


def _build_lineage(
    *,
    focus: LineageSnapshotKey,
    versions_by_kind: dict[CatalogKind, tuple[CatalogVersion, ...]],
    derived_versions: tuple[PublishedDerivedSnapshot, ...],
    buildable_derived_datasets: frozenset[DatasetId],
) -> DatasetLineage:
    snapshots = {
        _key(kind, snapshot): snapshot
        for kind, versions in versions_by_kind.items()
        for snapshot in versions
    }
    dependencies: dict[LineageSnapshotKey, set[LineageSnapshotKey]] = defaultdict(set)
    dependents: dict[LineageSnapshotKey, set[LineageSnapshotKey]] = defaultdict(set)
    for derived_snapshot in derived_versions:
        dependent = _key(CatalogKind.DERIVED, derived_snapshot)
        for reference in derived_snapshot.inputs:
            dependency = LineageSnapshotKey(
                _catalog_kind(reference.kind),
                reference.ref.dataset,
                reference.ref.version.value,
            )
            dependencies[dependent].add(dependency)
            dependents[dependency].add(dependent)

    connected = _connected_component(focus, dependencies, dependents)
    levels, has_cycle = _lineage_levels(connected, dependencies, dependents)
    latest = {
        (kind, item.dataset): item
        for kind, versions in versions_by_kind.items()
        for item in _latest_by_dataset(versions)
    }
    nodes = []
    for key in connected:
        actual_snapshot = snapshots.get(key)
        latest_snapshot = latest.get((key.kind, key.dataset))
        if actual_snapshot is None:
            freshness = DependencyFreshness.MISSING
        elif latest_snapshot is not None and latest_snapshot.version.value == key.version:
            freshness = DependencyFreshness.LATEST_REGISTERED
        else:
            freshness = DependencyFreshness.OLDER_VERSION
        nodes.append(
            LineageNode(
                key=key,
                snapshot=actual_snapshot,
                freshness=freshness,
                latest_version=(
                    latest_snapshot.version.value if latest_snapshot is not None else None
                ),
                dependencies=tuple(
                    LineageNodeReference(item, item in snapshots)
                    for item in sorted(dependencies[key] & connected, key=_key_sort)
                ),
                used_by=tuple(
                    LineageNodeReference(item, item in snapshots)
                    for item in sorted(dependents[key] & connected, key=_key_sort)
                ),
                level=levels[key],
            )
        )
    return DatasetLineage(
        focus,
        tuple(sorted(nodes, key=lambda item: (item.level, _key_sort(item.key)))),
        has_cycle,
        buildable_derived_datasets,
    )


def _connected_component(
    focus: LineageSnapshotKey,
    dependencies: dict[LineageSnapshotKey, set[LineageSnapshotKey]],
    dependents: dict[LineageSnapshotKey, set[LineageSnapshotKey]],
) -> set[LineageSnapshotKey]:
    connected: set[LineageSnapshotKey] = set()
    pending = deque((focus,))
    while pending:
        key = pending.popleft()
        if key in connected:
            continue
        connected.add(key)
        pending.extend(dependencies[key] | dependents[key])
    return connected


def _lineage_levels(
    connected: set[LineageSnapshotKey],
    dependencies: dict[LineageSnapshotKey, set[LineageSnapshotKey]],
    dependents: dict[LineageSnapshotKey, set[LineageSnapshotKey]],
) -> tuple[dict[LineageSnapshotKey, int], bool]:
    indegree = {
        key: len(dependencies[key] & connected)
        for key in connected
    }
    levels = {key: 0 for key in connected}
    pending = deque(sorted((key for key, degree in indegree.items() if degree == 0), key=_key_sort))
    visited: set[LineageSnapshotKey] = set()
    while pending:
        key = pending.popleft()
        visited.add(key)
        for dependent in sorted(dependents[key] & connected, key=_key_sort):
            levels[dependent] = max(levels[dependent], levels[key] + 1)
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                pending.append(dependent)
    remaining = connected - visited
    if remaining:
        fallback_level = max(levels.values(), default=0) + 1
        for key in sorted(remaining, key=_key_sort):
            levels[key] = fallback_level
    return levels, bool(remaining)


def _latest_by_dataset(
    versions: tuple[CatalogVersion, ...],
) -> tuple[CatalogVersion, ...]:
    latest: dict[DatasetId, CatalogVersion] = {}
    for item in versions:
        current = latest.get(item.dataset)
        if current is None or item.published_at > current.published_at:
            latest[item.dataset] = item
    return tuple(latest.values())


def _key(kind: CatalogKind, snapshot: CatalogVersion) -> LineageSnapshotKey:
    return LineageSnapshotKey(kind, snapshot.dataset, snapshot.version.value)


def _catalog_kind(kind: SnapshotKind) -> CatalogKind:
    return {
        SnapshotKind.SOURCE: CatalogKind.SOURCE,
        SnapshotKind.DERIVED: CatalogKind.DERIVED,
        SnapshotKind.EXTERNAL: CatalogKind.EXTERNAL,
    }[kind]


def _key_sort(key: LineageSnapshotKey) -> tuple[str, str, str, str]:
    return (key.dataset.source, key.dataset.dataset, key.kind.value, key.version)
