"""Verify exact historical lineage without reassessing upstream freshness."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ifx_registry.domain.catalog import (
    PublishedDerivedSnapshot,
    PublishedExternalDatasetVersion,
    PublishedSnapshot,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.models import SnapshotKind, SnapshotRef


@dataclass(frozen=True, slots=True)
class HistoricalLineageCheck:
    verified: tuple[tuple[RegisteredSnapshotRef, tuple[SnapshotRef, ...]], ...]
    failure: HistoricalLineageFailure | None = None


@dataclass(frozen=True, slots=True)
class HistoricalLineageFailure:
    reference: SnapshotRef
    reason: str
    missing: bool = False


def inspect_historical_lineage(
    snapshot: PublishedDerivedSnapshot,
    dependency: RegisteredSnapshotRef,
    get_snapshot: Callable[
        [SnapshotRef],
        PublishedSnapshot | PublishedDerivedSnapshot | PublishedExternalDatasetVersion | None,
    ],
) -> HistoricalLineageCheck:
    """Check a predecessor's full recorded input closure by exact identity."""
    verified: list[tuple[RegisteredSnapshotRef, tuple[SnapshotRef, ...]]] = []
    active = {SnapshotRef.derived(snapshot.snapshot_id)}
    seen: set[SnapshotRef] = set()

    def walk(
        parent: PublishedDerivedSnapshot,
        registered: RegisteredSnapshotRef,
    ) -> HistoricalLineageFailure | None:
        reference = registered.ref
        if reference in active:
            return HistoricalLineageFailure(
                reference, f"Historical dependency cycle detected at {reference.snapshot_id}"
            )
        predecessor = get_snapshot(reference)
        if predecessor is None:
            return HistoricalLineageFailure(
                reference, f"Historical input is not registered: {reference.snapshot_id}", True
            )
        if registered.manifest_uri != predecessor.manifest_uri or (
            registered.manifest_sha256 is not None
            and registered.manifest_sha256 != predecessor.manifest_sha256
        ):
            return HistoricalLineageFailure(reference, (
                "Historical input manifest identity does not match the "
                f"recorded input: {reference.snapshot_id}"
            ))
        if (
            reference.kind is SnapshotKind.DERIVED
            and reference.dataset == parent.dataset
            and predecessor.published_at >= parent.published_at
        ):
            return HistoricalLineageFailure(reference, (
                "Historical predecessor is not older than its dependent release: "
                f"{reference.snapshot_id}"
            ))
        if reference in seen:
            return None
        if not isinstance(predecessor, PublishedDerivedSnapshot):
            seen.add(reference)
            verified.append((registered, ()))
            return None
        active.add(reference)
        children = predecessor.inputs
        for child in children:
            failure = walk(predecessor, child)
            if failure is not None:
                return failure
        active.remove(reference)
        seen.add(reference)
        verified.append((registered, tuple(item.ref for item in children)))
        return None

    failure = walk(snapshot, dependency)
    return HistoricalLineageCheck(tuple(verified), failure)
