"""Exact-version dependency graph projections for Registry detail pages."""

import hashlib
from datetime import UTC, datetime
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any, cast

from ifx_registry.application.use_cases.browse_catalog import (
    BrowseRegistryCatalog,
    CatalogKind,
    DependencyFreshness,
    RebuildStatus,
)
from ifx_registry.application.use_cases.dataset_lineage import GetRegistryDatasetDetails
from ifx_registry.domain.catalog import (
    PublishedDerivedSnapshot,
    PublishedExternalDatasetVersion,
    PublishedFile,
    PublishedSnapshot,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    ExternalDatasetVersion,
    SnapshotRef,
    SourceVersion,
)


class Catalog:
    def __init__(self, *versions: object):
        self._versions = versions

    def list_all(self) -> tuple[object, ...]:
        return self._versions


def _file(name: str) -> PublishedFile:
    return PublishedFile(
        PurePosixPath(name),
        f"s3://registry/{name}",
        1,
        hashlib.sha256(b"x").hexdigest(),
    )


def _source(dataset: DatasetId, version: str, day: int) -> PublishedSnapshot:
    return PublishedSnapshot(
        dataset,
        SourceVersion(version),
        (_file(f"{dataset.dataset}-{version}.tsv"),),
        datetime(2026, 9, day, tzinfo=UTC),
        datetime(2026, 9, day, tzinfo=UTC),
        f"s3://registry/sources/{dataset}/{version}/manifest.yaml",
    )


def _external(dataset: DatasetId, version: str) -> PublishedExternalDatasetVersion:
    value = ExternalDatasetVersion(
        dataset,
        DatasetVersion(version),
        "api",
        "query",
        "Example service",
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    return PublishedExternalDatasetVersion(
        value,
        datetime(2026, 9, 1, tzinfo=UTC),
        f"s3://registry/external/{dataset}/{version}/manifest.yaml",
    )


def _derived(
    dataset: DatasetId,
    version: str,
    inputs: tuple[SnapshotRef, ...],
    day: int,
    metadata: dict[str, Any] | None = None,
) -> PublishedDerivedSnapshot:
    return PublishedDerivedSnapshot(
        dataset=dataset,
        version=DatasetVersion(version),
        files=(_file(f"{dataset.dataset}-{version}.tsv"),),
        inputs=tuple(
            RegisteredSnapshotRef(
                ref,
                f"s3://registry/{ref.kind.value}/{ref.snapshot_id}/manifest.yaml",
            )
            for ref in inputs
        ),
        producer=None,
        transform={},
        validation={},
        published_at=datetime(2026, 9, day, tzinfo=UTC),
        manifest_uri=f"s3://registry/derived/{dataset}/{version}/manifest.yaml",
        build_key=None,
        publication_fingerprint=str(day) * 64,
        metadata=metadata or {},
    )


def _query(
    sources: tuple[PublishedSnapshot, ...],
    derived: tuple[PublishedDerivedSnapshot, ...],
    external: tuple[PublishedExternalDatasetVersion, ...],
    recipe_datasets: tuple[DatasetId, ...] = (),
) -> GetRegistryDatasetDetails:
    recipes = SimpleNamespace(
        list_descriptors=lambda: tuple(
            SimpleNamespace(dataset=dataset) for dataset in recipe_datasets
        )
    )
    return GetRegistryDatasetDetails(
        cast(Any, Catalog(*sources)),
        cast(Any, Catalog(*derived)),
        cast(Any, Catalog(*external)),
        cast(Any, recipes),
    )


def test_every_page_in_a_component_gets_the_same_root_to_leaf_graph() -> None:
    root = DatasetId("source", "root")
    metadata = DatasetId("external", "metadata")
    middle = DatasetId("derived", "middle")
    leaf = DatasetId("derived", "leaf")
    source = _source(root, "1", 1)
    external = _external(metadata, "1")
    middle_snapshot = _derived(
        middle,
        "2",
        (SnapshotRef.source("source:root:1"), SnapshotRef.external("external:metadata:1")),
        2,
    )
    leaf_snapshot = _derived(
        leaf,
        "3",
        (SnapshotRef.derived("derived:middle:2"),),
        3,
    )
    query = _query((source,), (middle_snapshot, leaf_snapshot), (external,))

    root_details = query.execute(CatalogKind.SOURCE, root, "1")
    middle_details = query.execute(CatalogKind.DERIVED, middle, "2")
    leaf_details = query.execute(CatalogKind.DERIVED, leaf, "3")

    expected_keys = {node.key for node in root_details.lineage.nodes}
    assert expected_keys == {node.key for node in middle_details.lineage.nodes}
    assert expected_keys == {node.key for node in leaf_details.lineage.nodes}
    assert [len(level) for level in middle_details.lineage.levels] == [2, 1, 1]
    middle_node = next(
        node for node in middle_details.lineage.nodes if node.key == middle_details.lineage.focus
    )
    assert {item.key.snapshot_id for item in middle_node.dependencies} == {
        "source:root:1",
        "external:metadata:1",
    }
    assert [item.key.snapshot_id for item in middle_node.used_by] == ["derived:leaf:3"]


def test_missing_exact_input_remains_visible_without_becoming_a_registered_node() -> None:
    output = DatasetId("derived", "output")
    snapshot = _derived(
        output,
        "1",
        (SnapshotRef.source("source:missing:7"),),
        1,
    )

    details = _query((), (snapshot,), ()).execute(CatalogKind.DERIVED, output, "1")

    missing = next(node for node in details.lineage.nodes if node.is_missing)
    assert missing.key.snapshot_id == "source:missing:7"
    assert missing.used_by[0].exists
    output_node = next(node for node in details.lineage.nodes if not node.is_missing)
    assert output_node.dependencies[0].exists is False


def test_missing_derived_input_links_to_overview_when_recipe_is_installed() -> None:
    buildable_input = DatasetId("derived", "buildable")
    output = DatasetId("derived", "output")
    snapshot = _derived(
        output,
        "1",
        (SnapshotRef.derived("derived:buildable:deps-old"),),
        1,
    )

    details = _query((), (snapshot,), (), (buildable_input,)).execute(
        CatalogKind.DERIVED,
        output,
        "1",
    )

    groups = tuple(group for level in details.lineage.grouped_levels for group in level)
    buildable_group = next(group for group in groups if group.dataset == buildable_input)
    assert buildable_group.overview_available


def test_cycle_is_reported_and_traversal_terminates() -> None:
    first = DatasetId("derived", "first")
    second = DatasetId("derived", "second")
    first_snapshot = _derived(
        first,
        "1",
        (SnapshotRef.derived("derived:second:1"),),
        1,
    )
    second_snapshot = _derived(
        second,
        "1",
        (SnapshotRef.derived("derived:first:1"),),
        2,
    )

    details = _query((), (first_snapshot, second_snapshot), ()).execute(
        CatalogKind.DERIVED,
        first,
        "1",
    )

    assert details.lineage.has_cycle
    assert details.dataset.rebuild_status is RebuildStatus.UNKNOWN
    assert {node.key.snapshot_id for node in details.lineage.nodes} == {
        "derived:first:1",
        "derived:second:1",
    }


def test_legacy_malformed_service_observations_are_not_projected() -> None:
    output = DatasetId("derived", "output")
    snapshot = _derived(
        output,
        "1",
        (SnapshotRef.source("source:missing:7"),),
        1,
        metadata={"service_observations": ["bad", {"request_count": 2}]},
    )

    details = _query((), (snapshot,), ()).execute(CatalogKind.DERIVED, output, "1")
    output_node = next(node for node in details.lineage.nodes if not node.is_missing)

    assert output_node.service_observations == ()


def test_rebuild_status_propagates_through_latest_derived_inputs() -> None:
    source_id = DatasetId("source", "records")
    middle_id = DatasetId("derived", "middle")
    leaf_id = DatasetId("derived", "leaf")
    source_v1 = _source(source_id, "1", 1)
    source_v2 = _source(source_id, "2", 2)
    middle = _derived(
        middle_id,
        "1",
        (SnapshotRef.source("source:records:1"),),
        3,
    )
    leaf = _derived(
        leaf_id,
        "1",
        (SnapshotRef.derived("derived:middle:1"),),
        4,
    )
    catalog = BrowseRegistryCatalog(
        cast(Any, Catalog(source_v1, source_v2)),
        cast(Any, Catalog(middle, leaf)),
        cast(Any, Catalog()),
    ).execute()

    statuses = {
        item.dataset: item.rebuild_status
        for item in catalog
        if item.kind is CatalogKind.DERIVED
    }
    assert statuses == {
        middle_id: RebuildStatus.STALE,
        leaf_id: RebuildStatus.STALE,
    }
    lineages = {
        item.dataset: item.latest_lineage
        for item in catalog
        if item.kind is CatalogKind.DERIVED
    }
    assert lineages[middle_id] is not None
    assert (
        lineages[middle_id].dependencies[0].freshness
        is DependencyFreshness.OLDER_VERSION
    )
    assert lineages[leaf_id] is not None
    assert (
        lineages[leaf_id].dependencies[0].freshness
        is DependencyFreshness.LATEST_REGISTERED
    )
