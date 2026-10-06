"""Audit exact Registry references and their complete dependency closure."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ifx_registry.application.contracts import (
    DEFAULT_SOURCE_CHECK_FRESHNESS,
    DEFAULT_SOURCE_TIMEOUT,
)
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
    PublishedSnapshot,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.derived_builds import (
    DerivedRecipeDescriptor,
    managed_recipe_version,
)
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


class AuditCaveatCode(StrEnum):
    """A secondary limitation that does not erase a known primary action."""

    MANUAL_FRESHNESS = "manual_freshness"
    UPSTREAM_CHECK_UNVERIFIED = "upstream_check_unverified"


class SourceFreshnessBasis(StrEnum):
    """Evidence used to determine a source's freshness."""

    RECENT_REGISTERED_DOWNLOAD = "recent_registered_download"
    LIVE_UPSTREAM_CHECK = "live_upstream_check"


@dataclass(frozen=True, slots=True)
class AuditCaveat:
    """One transitive limitation on a freshness conclusion."""

    origin: SnapshotRef
    code: AuditCaveatCode
    message: str


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
    source_freshness_basis: SourceFreshnessBasis | None = None
    source_fresh_until: datetime | None = None
    reason: str = ""
    caveats: tuple[AuditCaveat, ...] = ()

    @property
    def is_current(self) -> bool:
        return self.disposition is AuditDisposition.CURRENT and not self.caveats

    @property
    def is_qualified_current(self) -> bool:
        """Whether the artifact is current under every automated policy."""
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
            and not item.caveats
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
        source_check_freshness: timedelta = DEFAULT_SOURCE_CHECK_FRESHNESS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if source_check_freshness <= timedelta(0):
            raise ValueError("source-check freshness must be positive")
        self._browse_catalog = browse_catalog
        self._check_source = check_source
        self._recipes = recipes
        self._source_check_freshness = source_check_freshness
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
        observed_at = self._clock()
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
            latest_snapshot = _latest_source_snapshot(dataset)
            if latest_snapshot is not None and _uses_download_date_version(latest_snapshot):
                fresh_until = latest_snapshot.downloaded_at + self._source_check_freshness
                if latest_snapshot.downloaded_at <= observed_at and observed_at < fresh_until:
                    latest_reference = SnapshotRef(
                        SnapshotKind.SOURCE,
                        latest_snapshot.dataset,
                        DatasetVersion(
                            latest_snapshot.version.value,
                            latest_snapshot.version.version_date,
                        ),
                    )
                    if reference.version.value != latest_snapshot.version.value:
                        return ReferenceAudit(
                            reference,
                            AuditDisposition.UPDATE_PIN,
                            pin_registered=pin_registered,
                            latest_registered_reference=latest_reference,
                            recommended_reference=latest_reference,
                            source_freshness_basis=(
                                SourceFreshnessBasis.RECENT_REGISTERED_DOWNLOAD
                            ),
                            source_fresh_until=fresh_until,
                            reason=(
                                "Use the newest registered source snapshot; upstream "
                                f"checking is deferred until {fresh_until.isoformat()}"
                            ),
                        )
                    return ReferenceAudit(
                        reference,
                        AuditDisposition.CURRENT,
                        pin_registered=True,
                        latest_registered_reference=latest_reference,
                        source_freshness_basis=(
                            SourceFreshnessBasis.RECENT_REGISTERED_DOWNLOAD
                        ),
                        source_fresh_until=fresh_until,
                        reason=(
                            "The exact pin is the newest registered source snapshot; "
                            f"upstream checking is deferred until {fresh_until.isoformat()}"
                        ),
                    )
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
                manual_caveat = _manual_freshness_caveat(reference, dataset)
                if manual_caveat is not None:
                    return ReferenceAudit(
                        reference,
                        AuditDisposition.UNVERIFIABLE,
                        pin_registered=pin_registered,
                        latest_registered_reference=latest,
                        reason=manual_caveat.message,
                        caveats=(manual_caveat,),
                    )
                check_caveat = _upstream_check_caveat(reference, checked)
                return ReferenceAudit(
                    reference,
                    AuditDisposition.UNVERIFIABLE,
                    pin_registered=True,
                    latest_registered_reference=latest,
                    reason=(
                        "The exact pin is registered, but live upstream freshness could "
                        "not be verified; registration order does not establish source "
                        "version order. Keep the pin and retry the upstream check."
                    ),
                    caveats=(check_caveat,),
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
                    source_freshness_basis=SourceFreshnessBasis.LIVE_UPSTREAM_CHECK,
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
                    source_freshness_basis=SourceFreshnessBasis.LIVE_UPSTREAM_CHECK,
                    reason=f"Use the checked and registered upstream version {checked.value}",
                )
            return ReferenceAudit(
                reference,
                AuditDisposition.CURRENT,
                pin_registered=pin_registered,
                latest_registered_reference=latest,
                latest_upstream_version=checked,
                source_freshness_basis=SourceFreshnessBasis.LIVE_UPSTREAM_CHECK,
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
            caveats = _merge_caveats(dependency_results)
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
                blocked_reason = cycle or _blocked_derived_reason(dependency_results)
                return ReferenceAudit(
                    reference,
                    AuditDisposition.BLOCKED,
                    dependencies,
                    pin_registered=True,
                    latest_registered_reference=latest,
                    reason=blocked_reason,
                    caveats=caveats,
                )
            hard_unverifiable = tuple(
                item
                for item in dependency_results
                if item.disposition is AuditDisposition.UNVERIFIABLE
                and not _has_only_manual_caveats(item)
            )
            if hard_unverifiable:
                origins = ", ".join(
                    item.reference.snapshot_id for item in hard_unverifiable
                )
                return ReferenceAudit(
                    reference,
                    AuditDisposition.UNVERIFIABLE,
                    dependencies,
                    pin_registered=True,
                    latest_registered_reference=latest,
                    reason=f"Dependency freshness cannot be verified: {origins}",
                    caveats=caveats,
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
                    caveats=caveats,
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
                slot = registered.slot or _recipe_slot_name(descriptor, target_reference)
                if slot is None:
                    return ReferenceAudit(
                        reference,
                        AuditDisposition.UNVERIFIABLE,
                        dependencies,
                        pin_registered=True,
                        latest_registered_reference=latest,
                        reason=(
                            "A legacy derived input has no recorded slot, and the "
                            "installed recipe cannot uniquely identify its role: "
                            f"{target_reference.snapshot_id}"
                        ),
                        caveats=caveats,
                    )
                target_inputs.append(
                    RegisteredSnapshotRef(
                        target_reference,
                        target_snapshot.manifest_uri,
                        target_snapshot.manifest_sha256,
                        slot,
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
                    if expected_result.is_qualified_current:
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
                            caveats=_merge_caveat_sets(
                                caveats,
                                expected_result.caveats,
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
                    caveats=caveats,
                )
            if caveats:
                origins = ", ".join(
                    caveat.origin.snapshot_id for caveat in caveats
                )
                if all(
                    caveat.code is AuditCaveatCode.MANUAL_FRESHNESS
                    for caveat in caveats
                ):
                    reason = (
                        "The managed derived snapshot matches the installed recipe and all "
                        "automatically checked inputs; freshness still depends on manual "
                        f"confirmation of {origins}"
                    )
                else:
                    reason = (
                        "The managed derived snapshot matches the installed recipe and "
                        "newest registered inputs; live upstream freshness could not be "
                        f"verified for {origins}"
                    )
            else:
                reason = "The managed derived snapshot and its complete lineage are current"
            return ReferenceAudit(
                reference,
                AuditDisposition.CURRENT,
                dependencies,
                pin_registered=True,
                latest_registered_reference=latest,
                reason=reason,
                caveats=caveats,
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
        return RegistryAudit(normalized_roots, tuple(ordered), observed_at)


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


def _latest_source_snapshot(
    dataset: CatalogDataset | None,
) -> PublishedSnapshot | None:
    if dataset is None:
        return None
    latest = dataset.latest
    return latest if isinstance(latest, PublishedSnapshot) else None


def _uses_download_date_version(snapshot: PublishedSnapshot) -> bool:
    """Whether upstream freshness is inferred from mutable file timestamps.

    Release, checksum, and manually assigned versions are always probed.  The
    short deferral only protects sources whose version derives from file or
    archive timestamp metadata, which can change when an unchanged archive is
    repackaged.
    """
    version_method = snapshot.metadata.get("version_method")
    if isinstance(version_method, Mapping):
        version_method = version_method.get("type")
    if isinstance(version_method, str) and version_method.startswith("manual_"):
        return False
    evidence = snapshot.version.evidence
    method_type = evidence.get("type")
    if method_type in {"last_modified", "zip_inner_file_timestamp"}:
        return True
    method = evidence.get("method")
    return isinstance(method, str) and method.endswith("_last_modified")


def _recipe_slot_name(
    descriptor: DerivedRecipeDescriptor,
    reference: SnapshotRef,
) -> str | None:
    """Supply a named slot for legacy manifests that did not persist one."""
    matches = tuple(
        slot.name
        for slot in descriptor.inputs
        if slot.kind is reference.kind and slot.dataset == reference.dataset
    )
    return matches[0] if len(matches) == 1 else None


def _manual_freshness_caveat(
    reference: SnapshotRef,
    dataset: CatalogDataset | None,
) -> AuditCaveat | None:
    snapshot = _catalog_version(dataset, reference.version.value)
    if not isinstance(snapshot, PublishedSnapshot):
        return None
    version_method = snapshot.metadata.get("version_method")
    method_type: object
    if isinstance(version_method, Mapping):
        method_type = version_method.get("type")
    else:
        method_type = version_method
    if not isinstance(method_type, str) or not method_type.startswith("manual_"):
        return None
    return AuditCaveat(
        reference,
        AuditCaveatCode.MANUAL_FRESHNESS,
        (
            f"No automatic upstream check is available for {reference.snapshot_id}; "
            "confirm that this exact manually versioned source is still current"
        ),
    )


def _upstream_check_caveat(
    reference: SnapshotRef,
    error: RegistryError,
) -> AuditCaveat:
    return AuditCaveat(
        reference,
        AuditCaveatCode.UPSTREAM_CHECK_UNVERIFIED,
        (
            f"Live upstream freshness could not be verified for {reference.snapshot_id}: "
            f"{error}"
        ),
    )


def _has_only_manual_caveats(item: ReferenceAudit) -> bool:
    return (
        item.reference.kind is SnapshotKind.SOURCE
        and bool(item.caveats)
        and all(
        caveat.code is AuditCaveatCode.MANUAL_FRESHNESS
        for caveat in item.caveats
        )
    )


def _merge_caveats(items: Sequence[ReferenceAudit]) -> tuple[AuditCaveat, ...]:
    return _merge_caveat_sets(*(item.caveats for item in items))


def _merge_caveat_sets(
    *groups: Sequence[AuditCaveat],
) -> tuple[AuditCaveat, ...]:
    return tuple(dict.fromkeys(caveat for group in groups for caveat in group))


def _blocked_derived_reason(items: Sequence[ReferenceAudit]) -> str:
    rebuilds = tuple(
        item.reference.snapshot_id
        for item in items
        if item.disposition is AuditDisposition.REBUILD_DERIVED
        or (
            item.disposition is AuditDisposition.BLOCKED
            and item.reason.startswith(("After rebuilding ", "After registering "))
        )
    )
    if rebuilds:
        return (
            f"After rebuilding {', '.join(rebuilds)}, rebuild this derived dataset"
        )
    registrations = tuple(
        _registration_target(item)
        for item in items
        if item.disposition is AuditDisposition.REGISTER_SOURCE
    )
    if registrations:
        return (
            f"After registering {', '.join(registrations)}, rebuild this derived dataset"
        )
    blocked = tuple(
        item.reference.snapshot_id
        for item in items
        if item.disposition is AuditDisposition.BLOCKED
    )
    return (
        f"Resolve blocked dependency {', '.join(blocked)}, then rebuild this derived dataset"
    )


def _registration_target(item: ReferenceAudit) -> str:
    if item.latest_upstream_version is None:
        return item.reference.snapshot_id
    return f"{item.reference.dataset}:{item.latest_upstream_version.value}"


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
