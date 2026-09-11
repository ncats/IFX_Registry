"""Deterministic identities for Registry-managed build plans."""

from dataclasses import replace

from ifx_registry.domain.catalog import RegisteredSnapshotRef
from ifx_registry.domain.derived_builds import (
    DerivedRecipeDescriptor,
    RecipeInputSlot,
    managed_recipe_version,
)
from ifx_registry.domain.models import DatasetId, ProducerIdentity, SnapshotKind, SnapshotRef


def _producer(revision: str = "a" * 40) -> ProducerIdentity:
    return ProducerIdentity(
        "registry",
        "1",
        "https://example.org/registry",
        revision,
    )


def _descriptor() -> DerivedRecipeDescriptor:
    return DerivedRecipeDescriptor(
        dataset=DatasetId("derived", "result"),
        display_name="Result",
        description="Combines exact inputs.",
        revision="1",
        inputs=(
            RecipeInputSlot(
                "records",
                "Records",
                SnapshotKind.SOURCE,
                DatasetId("source", "records"),
            ),
        ),
        producer=_producer(),
        transform={"name": "combine", "version": 1},
    )


def _input(version: str = "1", checksum: str = "b" * 64) -> RegisteredSnapshotRef:
    return RegisteredSnapshotRef(
        SnapshotRef.source(f"source:records:{version}"),
        f"s3://registry/sources/source/records/{version}/manifest.yaml",
        checksum,
        "records",
    )


def test_managed_version_is_stable_for_the_same_exact_plan() -> None:
    descriptor = _descriptor()

    assert managed_recipe_version(descriptor, (_input(),)) == managed_recipe_version(
        descriptor,
        (_input(),),
    )


def test_managed_version_changes_with_every_identity_input() -> None:
    descriptor = _descriptor()
    baseline = managed_recipe_version(descriptor, (_input(),))
    variants = (
        (descriptor, (_input(version="2"),)),
        (descriptor, (_input(checksum="c" * 64),)),
        (replace(descriptor, revision="2"), (_input(),)),
        (replace(descriptor, producer=_producer("d" * 40)), (_input(),)),
        (replace(descriptor, transform={"name": "combine", "version": 2}), (_input(),)),
        (descriptor, (replace(_input(), slot="other_slot"),)),
    )

    assert all(
        managed_recipe_version(candidate, inputs) != baseline
        for candidate, inputs in variants
    )
