"""Domain contracts for Registry-managed reusable derived dataset builds."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ifx_registry.domain.catalog import RegisteredSnapshotRef
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    ProducerIdentity,
    SnapshotKind,
)


@dataclass(frozen=True, slots=True)
class RecipeInputSlot:
    name: str
    label: str
    kind: SnapshotKind
    dataset: DatasetId

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.label.strip():
            raise ValueError("recipe input name and label must not be blank")


@dataclass(frozen=True, slots=True)
class DerivedRecipeDescriptor:
    dataset: DatasetId
    display_name: str
    description: str
    revision: str
    inputs: tuple[RecipeInputSlot, ...]
    producer: ProducerIdentity
    transform: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.display_name.strip() or not self.description.strip():
            raise ValueError("recipe display name and description must not be blank")
        if not self.revision.strip():
            raise ValueError("recipe revision must not be blank")
        if not self.inputs:
            raise ValueError("derived recipe must declare at least one input")
        names = [item.name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("recipe input names must be unique")


def effective_recipe_transform(descriptor: DerivedRecipeDescriptor) -> dict[str, Any]:
    """Return the exact transform identity used by managed builds and manifests."""

    return {
        **descriptor.transform,
        "recipe_revision": descriptor.revision,
    }


def registered_input_payload(value: RegisteredSnapshotRef) -> dict[str, Any]:
    """Return the canonical manifest representation of an exact dependency."""

    payload = {
        "kind": value.kind.value,
        "snapshot_id": value.snapshot_id,
        "source": value.ref.dataset.source,
        "dataset": value.ref.dataset.dataset,
        "version": value.ref.version.value,
        "manifest_uri": value.manifest_uri,
        "manifest_sha256": value.manifest_sha256,
    }
    if value.slot is not None:
        payload["slot"] = value.slot
    return payload


def registered_input_sort_key(value: RegisteredSnapshotRef) -> tuple[str, str, str]:
    """Order dependencies consistently across planning, publication, and reads."""

    return (value.slot or "", value.kind.value, value.snapshot_id)


def derived_publication_fingerprint(
    inputs: tuple[RegisteredSnapshotRef, ...],
    producer: ProducerIdentity | None,
    transform: Mapping[str, Any],
) -> str:
    """Identify exact inputs and producing code independently of output bytes."""

    payload = {
        "inputs": [
            registered_input_payload(item)
            for item in sorted(inputs, key=registered_input_sort_key)
        ],
        "producer": (
            {
                "name": producer.name,
                "release": producer.release,
                "code_repository": producer.code_repository,
                "code_revision": producer.code_revision,
            }
            if producer is not None
            else None
        ),
        "transform": dict(transform),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def managed_recipe_version(
    descriptor: DerivedRecipeDescriptor,
    inputs: tuple[RegisteredSnapshotRef, ...],
) -> DatasetVersion:
    """Compute the non-editable version of a Registry-managed recipe build."""

    fingerprint = derived_publication_fingerprint(
        inputs,
        descriptor.producer,
        effective_recipe_transform(descriptor),
    )
    return DatasetVersion(f"deps-{fingerprint[:12]}")
