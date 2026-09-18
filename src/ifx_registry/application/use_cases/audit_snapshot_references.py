"""Audit exact Registry references and their complete dependency closure."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ifx_registry.application.contracts import DEFAULT_SOURCE_TIMEOUT
from ifx_registry.application.ports.derived_builds import DerivedRecipeCatalog
from ifx_registry.application.use_cases.browse_catalog import (
    BrowseRegistryCatalog,
    CatalogDataset,
    CatalogKind,
)
from ifx_registry.application.use_cases.check_source_version import CheckSourceVersion
from ifx_registry.domain.catalog import (
    PublishedDatasetSnapshot,
    PublishedDerivedSnapshot,
    PublishedExternalDatasetVersion,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.derived_builds import managed_recipe_version
from ifx_registry.domain.errors import RegistryError
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    SnapshotKind,
    SnapshotRef,
    SourceVersion,
)


class AuditDisposition(StrEnum):
    """The next safe action for one exact reference."""

    CURRENT = "current"
    UPDATE_PIN = "update_pin"
    REGISTER_SOURCE = "register_source"
    REBUILD_DERIVED = "rebuild_derived"
    BLOCKED = "blocked"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class ReferenceAudit:
    """Caller-oriented freshness result for one exact Registry reference."""

    reference: SnapshotRef
    disposition: AuditDisposition
    dependencies: tuple[SnapshotRef, ...] = ()
    pin_registered: bool = False
    latest_registered_reference: SnapshotRef | None = None
    recommended_reference: SnapshotRef | None = None
    latest_upstream_version: SourceVersion | None = None
    reason: str = ""

    @property
    def is_current(self) -> bool:
        return self.disposition is AuditDisposition.CURRENT

    @property
    def pinned_snapshot_id(self) -> str:
        return self.reference.snapshot_id

    @property
    def latest_registered_snapshot_id(self) -> str | None:
        return (
            self.latest_registered_reference.snapshot_id
            if self.latest_registered_reference is not None
            else None
        )

    @property
    def recommended_snapshot_id(self) -> str | None:
        return (
            self.recommended_reference.snapshot_id
            if self.recommended_reference is not None
            else None
        )

    @property
    def registration_required(self) -> bool:
        return self.disposition is AuditDisposition.REGISTER_SOURCE

    @property
    def rebuild_required(self) -> bool:
        return self.disposition is AuditDisposition.REBUILD_DERIVED

    @property
    def action_required(self) -> bool:
        return self.disposition in {
            AuditDisposition.UPDATE_PIN,
            AuditDisposition.REGISTER_SOURCE,
            AuditDisposition.REBUILD_DERIVED,
        }


@dataclass(frozen=True, slots=True)
class RegistryAudit:
    """A coherent, dependency-first audit of one or more exact roots."""

    roots: tuple[SnapshotRef, ...]
    entries: tuple[ReferenceAudit, ...]
    observed_at: datetime

    @property
    def is_current(self) -> bool:
        return all(item.is_current for item in self.entries)

    @property
    def is_complete(self) -> bool:
        return all(
            item.disposition
            not in {AuditDisposition.BLOCKED, AuditDisposition.UNVERIFIABLE}
            for item in self.entries
        )

    @property
    def actions(self) -> tuple[ReferenceAudit, ...]:
        return tuple(item for item in self.entries if item.action_required)

    def for_reference(self, reference: SnapshotRef) -> ReferenceAudit:
        try:
            return next(item for item in self.entries if item.reference == reference)
        except StopIteration as error:
            raise KeyError(reference.snapshot_id) from error


class AuditSnapshotReferences:
    """Apply Registry freshness policy to an exact dependency closure."""

    def __init__(
        self,
        browse_catalog: BrowseRegistryCatalog,
        check_source: CheckSourceVersion,
        recipes: DerivedRecipeCatalog,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._browse_catalog = browse_catalog
        self._check_source = check_source
        self._recipes = recipes
        self._clock = clock

    def execute(
        self,
        roots: Sequence[SnapshotRef],
        *,
        timeout: timedelta = DEFAULT_SOURCE_TIMEOUT,
    ) -> RegistryAudit:
        normalized_roots = tuple(dict.fromkeys(roots))
        catalog = self._browse_catalog.execute()
        datasets = {(item.kind, item.dataset): item for item in catalog}
        recipes = {
            descriptor.dataset: descriptor
            for descriptor in self._recipes.list_descriptors()
        }
        source_checks: dict[DatasetId, SourceVersion | RegistryError] = {}
        results: dict[SnapshotRef, ReferenceAudit] = {}
        ordered: list[ReferenceAudit] = []
        active: list[SnapshotRef] = []

        def visit(reference: SnapshotRef) -> ReferenceAudit:
            cached = results.get(reference)
            if cached is not None:
                return cached
            if reference in active:
                start = active.index(reference)
                cycle = " -> ".join(
                    item.snapshot_id for item in (*active[start:], reference)
                )
                return ReferenceAudit(
                    reference,
                    AuditDisposition.BLOCKED,
                    reason=f"Dependency cycle detected: {cycle}",
                )

            active.append(reference)
            dataset = datasets.get((_catalog_kind(reference.kind), reference.dataset))
            if reference.kind is SnapshotKind.SOURCE:
                result = assess_source(reference, dataset)
            elif reference.kind is SnapshotKind.DERIVED:
                result = assess_derived(reference, dataset)
            else:
                result = assess_external(reference, dataset)
            active.pop()
            results[reference] = result
            ordered.append(result)
            return result

        def assess_source(
            reference: SnapshotRef,
            dataset: CatalogDataset | None,
        ) -> ReferenceAudit:
            latest = _latest_reference(reference.kind, dataset)
            registered_versions = _registered_versions(dataset)
            pin_registered = reference.version.value in registered_versions
            checked = source_checks.get(reference.dataset)
            if checked is None:
                try:
                    checked = self._check_source.execute(
                        reference.dataset,
                        timeout=timeout,
                    )
                except RegistryError as error:
                    checked = error
                source_checks[reference.dataset] = checked
            if isinstance(checked, RegistryError):
                if not pin_registered:
                    return ReferenceAudit(
                        reference,
                        AuditDisposition.BLOCKED,
                        pin_registered=False,
                        latest_registered_reference=latest,
                        reason=(
                            "The exact source pin is not registered, and its upstream "
                            f"version cannot be checked automatically: {checked}"
                        ),
                    )
                return ReferenceAudit(
                    reference,
                    AuditDisposition.UNVERIFIABLE,
                    pin_registered=pin_registered,
                    latest_registered_reference=latest,
                    reason=f"Automatic upstream check unavailable: {checked}",
                )
            upstream = SnapshotRef(
                SnapshotKind.SOURCE,
                reference.dataset,
                DatasetVersion(checked.value, checked.version_date),
            )
            if checked.value not in registered_versions:
                return ReferenceAudit(
                    reference,
                    AuditDisposition.REGISTER_SOURCE,
                    pin_registered=pin_registered,
                    latest_registered_reference=latest,
                    latest_upstream_version=checked,
                    reason=(
                        f"Upstream version {checked.value} is not registered; "
                        "register it in IFX Registry before changing the consumer pin"
                    ),
                )
            if reference.version.value != checked.value:
                return ReferenceAudit(
                    reference,
                    AuditDisposition.UPDATE_PIN,
                    pin_registered=pin_registered,
                    latest_registered_reference=latest,
                    recommended_reference=upstream,
                    latest_upstream_version=checked,
                    reason=f"Use the checked and registered upstream version {checked.value}",
                )
            return ReferenceAudit(
                reference,
                AuditDisposition.CURRENT,
                pin_registered=pin_registered,
                latest_registered_reference=latest,
                latest_upstream_version=checked,
                reason="The exact pin is registered and matches the checked upstream version",
            )

        def assess_derived(
            reference: SnapshotRef,
            dataset: CatalogDataset | None,
        ) -> ReferenceAudit:
            latest = _latest_reference(reference.kind, dataset)
            snapshot = _derived_snapshot(dataset, reference.version.value)
            if snapshot is None:
                return ReferenceAudit(
                    reference,
                    AuditDisposition.BLOCKED,
                    latest_registered_reference=latest,
                    reason="The exact derived snapshot is not registered",
                )
            dependencies = tuple(item.ref for item in snapshot.inputs)
            dependency_results = tuple(visit(item) for item in dependencies)
            if any(
                item.disposition is AuditDisposition.UNVERIFIABLE
                for item in dependency_results
            ):
                return ReferenceAudit(
                    reference,
                    AuditDisposition.UNVERIFIABLE,
                    dependencies,
                    pin_registered=True,
                    latest_registered_reference=latest,
                    reason="At least one dependency cannot be verified",
                )
            blocking_dispositions = {
                AuditDisposition.REGISTER_SOURCE,
                AuditDisposition.REBUILD_DERIVED,
                AuditDisposition.BLOCKED,
            }
            if any(
                item.disposition in blocking_dispositions
                for item in dependency_results
            ):
                cycle = next(
                    (
                        item.reason
                        for item in dependency_results
                        if item.reason.startswith("Dependency cycle detected:")
                    ),
                    None,
                )
                return ReferenceAudit(
                    reference,
                    AuditDisposition.BLOCKED,
                    dependencies,
                    pin_registered=True,
                    latest_registered_reference=latest,
                    reason=cycle
                    or (
                        "Update or register the dependency actions listed above, "
                        "then rebuild this derived dataset"
                    ),
                )
            descriptor = recipes.get(reference.dataset)
            if descriptor is None:
                return ReferenceAudit(
                    reference,
                    AuditDisposition.UNVERIFIABLE,
                    dependencies,
                    pin_registered=True,
                    latest_registered_reference=latest,
                    reason=(
                        "No installed Registry recipe defines freshness for this "
                        "caller-produced derived dataset"
                    ),
                )
            target_inputs: list[RegisteredSnapshotRef] = []
            for registered, result in zip(
                snapshot.inputs,
                dependency_results,
                strict=True,
            ):
                target_reference = result.recommended_reference or registered.ref
                target_dataset = datasets.get(
                    (_catalog_kind(target_reference.kind), target_reference.dataset)
                )
                target_snapshot = _catalog_version(
                    target_dataset,
                    target_reference.version.value,
                )
                if target_snapshot is None:
                    return ReferenceAudit(
                        reference,
                        AuditDisposition.BLOCKED,
                        dependencies,
                        pin_registered=True,
                        latest_registered_reference=latest,
                        reason=(
                            "A recommended dependency snapshot is not registered: "
                            f"{target_reference.snapshot_id}"
                        ),
                    )
                target_inputs.append(
                    RegisteredSnapshotRef(
                        target_reference,
                        target_snapshot.manifest_uri,
                        target_snapshot.manifest_sha256,
                        registered.slot,
                    )
                )
            expected_version = managed_recipe_version(
                descriptor,
                tuple(target_inputs),
            ).value
            if expected_version != reference.version.value:
                if expected_version in _registered_versions(dataset):
                    expected_reference = SnapshotRef(
                        SnapshotKind.DERIVED,
                        reference.dataset,
                        DatasetVersion(expected_version),
                    )
                    expected_result = visit(expected_reference)
                    if expected_result.is_current:
                        return ReferenceAudit(
                            reference,
                            AuditDisposition.UPDATE_PIN,
                            dependencies,
                            pin_registered=True,
                            latest_registered_reference=latest,
                            recommended_reference=expected_reference,
                            reason=(
                                "A registered derived snapshot matches the installed "
                                "recipe revision and current inputs"
                            ),
                        )
                return ReferenceAudit(
                    reference,
                    AuditDisposition.REBUILD_DERIVED,
                    dependencies,
                    pin_registered=True,
                    latest_registered_reference=latest,
                    reason=(
                        f"Current registered inputs and the installed recipe revision produce "
                        f"{expected_version}; rebuild this dataset in IFX Registry"
                    ),
                )
            return ReferenceAudit(
                reference,
                AuditDisposition.CURRENT,
                dependencies,
                pin_registered=True,
                latest_registered_reference=latest,
                reason="The managed derived snapshot and its complete lineage are current",
            )

        def assess_external(
            reference: SnapshotRef,
            dataset: CatalogDataset | None,
        ) -> ReferenceAudit:
            latest = _latest_reference(reference.kind, dataset)
            pin_registered = reference.version.value in _registered_versions(dataset)
            if not pin_registered:
                return ReferenceAudit(
                    reference,
                    AuditDisposition.BLOCKED,
                    latest_registered_reference=latest,
                    reason="The exact external dataset version is not registered",
                )
            return ReferenceAudit(
                reference,
                AuditDisposition.UNVERIFIABLE,
                pin_registered=True,
                latest_registered_reference=latest,
                reason=(
                    "No live external-version checker is installed; catalog recency "
                    "does not prove upstream freshness"
                ),
            )

        for root in normalized_roots:
            visit(root)
        return RegistryAudit(normalized_roots, tuple(ordered), self._clock())


def _catalog_kind(kind: SnapshotKind) -> CatalogKind:
    if kind is SnapshotKind.SOURCE:
        return CatalogKind.SOURCE
    if kind is SnapshotKind.DERIVED:
        return CatalogKind.DERIVED
    return CatalogKind.EXTERNAL


def _registered_versions(dataset: CatalogDataset | None) -> frozenset[str]:
    if dataset is None:
        return frozenset()
    return frozenset(item.version.value for item in dataset.versions)


def _latest_reference(
    kind: SnapshotKind,
    dataset: CatalogDataset | None,
) -> SnapshotRef | None:
    if dataset is None:
        return None
    return SnapshotRef(kind, dataset.dataset, DatasetVersion(dataset.latest.version.value))


def _derived_snapshot(
    dataset: CatalogDataset | None,
    version: str,
) -> PublishedDerivedSnapshot | None:
    if dataset is None:
        return None
    return next(
        (
            item
            for item in dataset.versions
            if isinstance(item, PublishedDerivedSnapshot) and item.version.value == version
        ),
        None,
    )


def _catalog_version(
    dataset: CatalogDataset | None,
    version: str,
) -> PublishedDatasetSnapshot | PublishedExternalDatasetVersion | None:
    if dataset is None:
        return None
    return next(
        (item for item in dataset.versions if item.version.value == version),
        None,
    )
