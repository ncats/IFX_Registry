"""Dependency-complete freshness audits for Registry consumers."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, cast

from ifx_registry.application.use_cases.audit_snapshot_references import (
    AuditCaveatCode,
    AuditDisposition,
    AuditSnapshotReferences,
)
from ifx_registry.application.use_cases.browse_catalog import (
    BrowseRegistryCatalog,
    CatalogDataset,
    CatalogKind,
)
from ifx_registry.domain.catalog import (
    PublishedDerivedSnapshot,
    PublishedFile,
    PublishedSnapshot,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.derived_builds import (
    DerivedRecipeDescriptor,
    RecipeInputSlot,
    managed_recipe_version,
)
from ifx_registry.domain.errors import UnknownSourceError
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    ProducerIdentity,
    SnapshotKind,
    SnapshotRef,
    SourceVersion,
)


def _file() -> PublishedFile:
    return PublishedFile(
        PurePosixPath("records.tsv"),
        "s3://registry/records.tsv",
        1,
        "a" * 64,
    )


def _source(
    dataset: DatasetId,
    version: str,
    day: int,
    *,
    metadata: dict[str, Any] | None = None,
) -> PublishedSnapshot:
    return PublishedSnapshot(
        dataset,
        SourceVersion(version),
        (_file(),),
        datetime(2026, 9, day, tzinfo=UTC),
        datetime(2026, 9, day, tzinfo=UTC),
        f"s3://registry/sources/{dataset.source}/{dataset.dataset}/{version}/manifest.yaml",
        metadata=metadata or {},
        manifest_sha256="b" * 64,
    )


def _registered(reference: SnapshotRef, slot: str) -> RegisteredSnapshotRef:
    root = "sources" if reference.kind is SnapshotKind.SOURCE else "derived"
    return RegisteredSnapshotRef(
        reference,
        f"s3://registry/{root}/{reference.dataset.source}/"
        f"{reference.dataset.dataset}/{reference.version.value}/manifest.yaml",
        "b" * 64,
        slot,
    )


def _descriptor(
    dataset: DatasetId,
    input_dataset: DatasetId,
    *,
    input_kind: SnapshotKind = SnapshotKind.SOURCE,
) -> DerivedRecipeDescriptor:
    return DerivedRecipeDescriptor(
        dataset,
        "Example output",
        "Derived test output.",
        "2",
        (
            RecipeInputSlot(
                "records",
                "Records",
                input_kind,
                input_dataset,
            ),
        ),
        ProducerIdentity("registry", "1", "https://example.org/repo", "c" * 40),
        {"name": "example"},
    )


def _derived(
    descriptor: DerivedRecipeDescriptor,
    dependency: SnapshotRef,
    *,
    version: str | None = None,
) -> PublishedDerivedSnapshot:
    inputs = (_registered(dependency, "records"),)
    resolved_version = version or managed_recipe_version(descriptor, inputs).value
    return PublishedDerivedSnapshot(
        descriptor.dataset,
        DatasetVersion(resolved_version),
        (_file(),),
        inputs,
        descriptor.producer,
        descriptor.transform,
        {},
        datetime(2026, 9, 10, tzinfo=UTC),
        f"s3://registry/derived/{descriptor.dataset}/{resolved_version}/manifest.yaml",
        build_key=None,
        publication_fingerprint="d" * 64,
    )


def _caller_derived(dataset: DatasetId, dependency: SnapshotRef) -> PublishedDerivedSnapshot:
    return PublishedDerivedSnapshot(
        dataset,
        DatasetVersion("1"),
        (_file(),),
        (_registered(dependency, "input"),),
        None,
        {"name": "caller"},
        {},
        datetime(2026, 9, 10, tzinfo=UTC),
        f"s3://registry/derived/{dataset}/1/manifest.yaml",
        build_key=None,
        publication_fingerprint="e" * 64,
    )


class Browse:
    def __init__(self, datasets: tuple[CatalogDataset, ...]):
        self.datasets = datasets
        self.calls = 0

    def execute(self) -> tuple[CatalogDataset, ...]:
        self.calls += 1
        return self.datasets


class Check:
    def __init__(self, versions: dict[DatasetId, str | Exception]):
        self.versions = versions
        self.calls: list[DatasetId] = []

    def execute(self, dataset: DatasetId, *, timeout: object) -> SourceVersion:
        del timeout
        self.calls.append(dataset)
        value = self.versions[dataset]
        if isinstance(value, Exception):
            raise value
        return SourceVersion(value)


class Recipes:
    def __init__(self, *descriptors: DerivedRecipeDescriptor):
        self.descriptors = descriptors

    def list_descriptors(self) -> tuple[DerivedRecipeDescriptor, ...]:
        return self.descriptors


def _source_dataset(*snapshots: PublishedSnapshot) -> CatalogDataset:
    return CatalogDataset(CatalogKind.SOURCE, snapshots[0].dataset, snapshots)


def _derived_dataset(*snapshots: PublishedDerivedSnapshot) -> CatalogDataset:
    return CatalogDataset(CatalogKind.DERIVED, snapshots[0].dataset, snapshots)


def test_audit_reads_catalog_once_deduplicates_checks_and_orders_dependencies_first() -> None:
    source_id = DatasetId("example", "records")
    output_id = DatasetId("derived", "output")
    descriptor = _descriptor(output_id, source_id)
    source = _source(source_id, "2", 2)
    derived = _derived(descriptor, SnapshotRef.source("example:records:2"))
    browse = Browse((_source_dataset(source), _derived_dataset(derived)))
    check = Check({source_id: "2"})
    root = SnapshotRef.derived(derived.snapshot_id)

    report = AuditSnapshotReferences(
        cast(BrowseRegistryCatalog, browse),
        cast(Any, check),
        cast(Any, Recipes(descriptor)),
        clock=lambda: datetime(2026, 9, 18, tzinfo=UTC),
    ).execute((root, SnapshotRef.source("example:records:2")))

    assert browse.calls == 1
    assert check.calls == [source_id]
    assert [item.reference.snapshot_id for item in report.entries] == [
        "example:records:2",
        derived.snapshot_id,
    ]
    assert report.is_current
    assert report.is_complete
    assert report.observed_at == datetime(2026, 9, 18, tzinfo=UTC)


def test_unregistered_upstream_source_blocks_derived_rebuild_in_action_order() -> None:
    source_id = DatasetId("example", "records")
    output_id = DatasetId("derived", "output")
    descriptor = _descriptor(output_id, source_id)
    source = _source(source_id, "1", 1)
    derived = _derived(descriptor, SnapshotRef.source("example:records:1"))
    root = SnapshotRef.derived(derived.snapshot_id)

    report = AuditSnapshotReferences(
        cast(BrowseRegistryCatalog, Browse((_source_dataset(source), _derived_dataset(derived)))),
        cast(Any, Check({source_id: "2"})),
        cast(Any, Recipes(descriptor)),
    ).execute((root,))

    source_result, derived_result = report.entries
    assert source_result.disposition is AuditDisposition.REGISTER_SOURCE
    assert source_result.latest_upstream_version is not None
    assert source_result.latest_upstream_version.value == "2"
    assert derived_result.disposition is AuditDisposition.BLOCKED
    assert report.actions == (source_result,)
    assert not report.is_current


def test_registration_prerequisite_propagates_through_deep_rebuild_chain() -> None:
    source_id = DatasetId("example", "records")
    first_id = DatasetId("derived", "first")
    second_id = DatasetId("derived", "second")
    third_id = DatasetId("derived", "third")
    first_descriptor = _descriptor(first_id, source_id)
    second_descriptor = _descriptor(
        second_id,
        first_id,
        input_kind=SnapshotKind.DERIVED,
    )
    third_descriptor = _descriptor(
        third_id,
        second_id,
        input_kind=SnapshotKind.DERIVED,
    )
    source = _source(source_id, "1", 1)
    first = _derived(first_descriptor, SnapshotRef.source("example:records:1"))
    second = _derived(second_descriptor, SnapshotRef.derived(first.snapshot_id))
    third = _derived(third_descriptor, SnapshotRef.derived(second.snapshot_id))

    report = AuditSnapshotReferences(
        cast(
            BrowseRegistryCatalog,
            Browse(
                (
                    _source_dataset(source),
                    _derived_dataset(first),
                    _derived_dataset(second),
                    _derived_dataset(third),
                )
            ),
        ),
        cast(Any, Check({source_id: "2"})),
        cast(Any, Recipes(first_descriptor, second_descriptor, third_descriptor)),
    ).execute((SnapshotRef.derived(third.snapshot_id),))

    source_result, first_result, second_result, third_result = report.entries
    assert source_result.disposition is AuditDisposition.REGISTER_SOURCE
    assert first_result.reason.startswith("After registering example:records:2")
    assert second_result.reason.startswith(f"After rebuilding {first.snapshot_id}")
    assert third_result.reason.startswith(f"After rebuilding {second.snapshot_id}")
    assert all(
        item.disposition is AuditDisposition.BLOCKED
        for item in (first_result, second_result, third_result)
    )


def test_recipe_revision_drift_requires_rebuild_even_with_current_inputs() -> None:
    source_id = DatasetId("example", "records")
    output_id = DatasetId("derived", "output")
    descriptor = _descriptor(output_id, source_id)
    source = _source(source_id, "1", 1)
    derived = _derived(
        descriptor,
        SnapshotRef.source("example:records:1"),
        version="deps-oldrecipe",
    )

    report = AuditSnapshotReferences(
        cast(BrowseRegistryCatalog, Browse((_source_dataset(source), _derived_dataset(derived)))),
        cast(Any, Check({source_id: "1"})),
        cast(Any, Recipes(descriptor)),
    ).execute((SnapshotRef.derived(derived.snapshot_id),))

    result = report.entries[-1]
    assert result.disposition is AuditDisposition.REBUILD_DERIVED
    assert "recipe revision" in result.reason


def test_recipe_revision_drift_recommends_matching_registered_output() -> None:
    source_id = DatasetId("example", "records")
    output_id = DatasetId("derived", "output")
    current_descriptor = _descriptor(output_id, source_id)
    old_descriptor = replace(current_descriptor, revision="1")
    source = _source(source_id, "1", 1)
    dependency = SnapshotRef.source("example:records:1")
    old = _derived(old_descriptor, dependency)
    current = _derived(current_descriptor, dependency)
    reference = SnapshotRef.derived(old.snapshot_id)

    report = AuditSnapshotReferences(
        cast(
            BrowseRegistryCatalog,
            Browse((_source_dataset(source), _derived_dataset(current, old))),
        ),
        cast(Any, Check({source_id: "1"})),
        cast(Any, Recipes(current_descriptor)),
    ).execute((reference,))

    result = report.for_reference(reference)
    assert result.disposition is AuditDisposition.UPDATE_PIN
    assert result.recommended_reference == SnapshotRef.derived(current.snapshot_id)


def test_manual_source_and_caller_derived_are_unverifiable_results_not_exceptions() -> None:
    source_id = DatasetId("manual", "records")
    output_id = DatasetId("collaborator", "output")
    source = _source(source_id, "1", 1)
    descriptor = _descriptor(output_id, source_id)
    derived = _derived(descriptor, SnapshotRef.source("manual:records:1"))

    report = AuditSnapshotReferences(
        cast(BrowseRegistryCatalog, Browse((_source_dataset(source), _derived_dataset(derived)))),
        cast(Any, Check({source_id: UnknownSourceError("no automatic checker")})),
        cast(Any, Recipes()),
    ).execute((SnapshotRef.derived(derived.snapshot_id),))

    assert [item.disposition for item in report.entries] == [
        AuditDisposition.UNVERIFIABLE,
        AuditDisposition.UNVERIFIABLE,
    ]
    assert not report.is_complete


def test_managed_derived_can_be_current_with_a_manual_source_caveat() -> None:
    source_id = DatasetId("manual", "records")
    output_id = DatasetId("derived", "output")
    source = _source(
        source_id,
        "1",
        1,
        metadata={"version_method": {"type": "manual_provider_release"}},
    )
    descriptor = _descriptor(output_id, source_id)
    derived = _derived(descriptor, SnapshotRef.source("manual:records:1"))
    root = SnapshotRef.derived(derived.snapshot_id)

    report = AuditSnapshotReferences(
        cast(BrowseRegistryCatalog, Browse((_source_dataset(source), _derived_dataset(derived)))),
        cast(Any, Check({source_id: UnknownSourceError("no automatic checker")})),
        cast(Any, Recipes(descriptor)),
    ).execute((root,))

    source_result, derived_result = report.entries
    assert source_result.disposition is AuditDisposition.UNVERIFIABLE
    assert source_result.caveats[0].code is AuditCaveatCode.MANUAL_FRESHNESS
    assert derived_result.disposition is AuditDisposition.CURRENT
    assert derived_result.caveats == source_result.caveats
    assert derived_result.is_qualified_current
    assert not derived_result.is_current
    assert not report.is_current
    assert not report.is_complete


def test_known_input_update_outweighs_a_manual_source_caveat() -> None:
    manual_id = DatasetId("manual", "records")
    automatic_id = DatasetId("automatic", "records")
    output_id = DatasetId("derived", "combined")
    descriptor = DerivedRecipeDescriptor(
        output_id,
        "Combined output",
        "Derived from manual and automatic records.",
        "1",
        (
            RecipeInputSlot("manual", "Manual records", SnapshotKind.SOURCE, manual_id),
            RecipeInputSlot(
                "automatic",
                "Automatic records",
                SnapshotKind.SOURCE,
                automatic_id,
            ),
        ),
        ProducerIdentity("registry", "1", "https://example.org/repo", "c" * 40),
        {"name": "combined"},
    )
    manual = _source(
        manual_id,
        "1",
        1,
        metadata={"version_method": {"type": "manual_provider_release"}},
    )
    automatic_v1 = _source(automatic_id, "1", 1)
    automatic_v2 = _source(automatic_id, "2", 2)
    inputs = (
        _registered(SnapshotRef.source("manual:records:1"), "manual"),
        _registered(SnapshotRef.source("automatic:records:1"), "automatic"),
    )
    derived = PublishedDerivedSnapshot(
        output_id,
        managed_recipe_version(descriptor, inputs),
        (_file(),),
        inputs,
        descriptor.producer,
        descriptor.transform,
        {},
        datetime(2026, 9, 10, tzinfo=UTC),
        "s3://registry/derived/derived/combined/deps-old/manifest.yaml",
        build_key=None,
        publication_fingerprint="d" * 64,
    )

    report = AuditSnapshotReferences(
        cast(
            BrowseRegistryCatalog,
            Browse(
                (
                    _source_dataset(manual),
                    _source_dataset(automatic_v2, automatic_v1),
                    _derived_dataset(derived),
                )
            ),
        ),
        cast(
            Any,
            Check(
                {
                    manual_id: UnknownSourceError("no automatic checker"),
                    automatic_id: "2",
                }
            ),
        ),
        cast(Any, Recipes(descriptor)),
    ).execute((SnapshotRef.derived(derived.snapshot_id),))

    derived_result = report.entries[-1]
    assert derived_result.disposition is AuditDisposition.REBUILD_DERIVED
    assert [caveat.origin.snapshot_id for caveat in derived_result.caveats] == [
        "manual:records:1"
    ]
    assert report.actions[-1] is derived_result


def test_missing_manual_source_pin_is_blocked_even_when_check_is_unavailable() -> None:
    source_id = DatasetId("manual", "records")
    source = _source(source_id, "1", 1)
    reference = SnapshotRef.source("manual:records:missing")

    result = AuditSnapshotReferences(
        cast(BrowseRegistryCatalog, Browse((_source_dataset(source),))),
        cast(Any, Check({source_id: UnknownSourceError("no automatic checker")})),
        cast(Any, Recipes()),
    ).execute((reference,)).for_reference(reference)

    assert result.disposition is AuditDisposition.BLOCKED
    assert not result.pin_registered
    assert "not registered" in result.reason
    assert "cannot be checked automatically" in result.reason


def test_registered_newer_source_makes_managed_derived_rebuild_actionable() -> None:
    source_id = DatasetId("example", "records")
    output_id = DatasetId("derived", "output")
    descriptor = _descriptor(output_id, source_id)
    source_v1 = _source(source_id, "1", 1)
    source_v2 = _source(source_id, "2", 2)
    derived = _derived(descriptor, SnapshotRef.source("example:records:1"))
    root = SnapshotRef.derived(derived.snapshot_id)

    report = AuditSnapshotReferences(
        cast(
            BrowseRegistryCatalog,
            Browse(
                (
                    _source_dataset(source_v2, source_v1),
                    _derived_dataset(derived),
                )
            ),
        ),
        cast(Any, Check({source_id: "2"})),
        cast(Any, Recipes(descriptor)),
    ).execute((root,))

    source_result, derived_result = report.entries
    assert source_result.disposition is AuditDisposition.UPDATE_PIN
    assert derived_result.disposition is AuditDisposition.REBUILD_DERIVED
    assert derived_result.rebuild_required
    assert report.actions == (source_result, derived_result)


def test_missing_exact_pin_is_distinct_from_an_older_registered_pin() -> None:
    source_id = DatasetId("example", "records")
    source = _source(source_id, "2", 2)
    reference = SnapshotRef.source("example:records:missing")

    result = AuditSnapshotReferences(
        cast(BrowseRegistryCatalog, Browse((_source_dataset(source),))),
        cast(Any, Check({source_id: "2"})),
        cast(Any, Recipes()),
    ).execute((reference,)).for_reference(reference)

    assert not result.pin_registered
    assert result.disposition is AuditDisposition.UPDATE_PIN
    assert result.recommended_reference == SnapshotRef.source("example:records:2")


def test_dependency_cycle_is_reported_deterministically() -> None:
    first_id = DatasetId("derived", "first")
    second_id = DatasetId("derived", "second")
    first = _caller_derived(first_id, SnapshotRef.derived("derived:second:1"))
    second = _caller_derived(second_id, SnapshotRef.derived("derived:first:1"))

    report = AuditSnapshotReferences(
        cast(
            BrowseRegistryCatalog,
            Browse((_derived_dataset(first), _derived_dataset(second))),
        ),
        cast(Any, Check({})),
        cast(Any, Recipes()),
    ).execute((SnapshotRef.derived(first.snapshot_id),))

    assert [item.reference.snapshot_id for item in report.entries] == [
        second.snapshot_id,
        first.snapshot_id,
    ]
    assert all(item.disposition is AuditDisposition.BLOCKED for item in report.entries)
    assert all(
        item.reason
        == "Dependency cycle detected: derived:first:1 -> derived:second:1 -> derived:first:1"
        for item in report.entries
    )
