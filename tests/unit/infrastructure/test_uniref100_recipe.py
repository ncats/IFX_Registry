from __future__ import annotations

import csv
import gzip
from pathlib import Path

import pytest

from ifx_registry.application.derived_build_models import MaterializedRecipeInput
from ifx_registry.application.progress import NullProgressReporter
from ifx_registry.domain.catalog import RegisteredSnapshotRef
from ifx_registry.domain.errors import InvalidDerivedBuildError
from ifx_registry.domain.models import DatasetId, SnapshotKind, SnapshotRef
from ifx_registry.infrastructure.recipes.uniref100 import UniRef100MembershipsRecipe


def _inputs(
    recipe: UniRef100MembershipsRecipe,
    root: Path,
    rows: list[tuple[str, str, str]],
) -> dict[str, MaterializedRecipeInput]:
    source_dir = root / "human_idmapping"
    source_dir.mkdir(parents=True)
    with gzip.open(source_dir / "HUMAN_9606_idmapping.dat.gz", "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write("\t".join(row) + "\n")
    slot = recipe.descriptor.inputs[0]
    reference = RegisteredSnapshotRef(
        SnapshotRef.source("uniprot:human_idmapping:2026_03"),
        "s3://bucket/human_idmapping/manifest.yaml",
    )
    return {slot.name: MaterializedRecipeInput(slot, reference, source_dir)}


def test_descriptor_depends_only_on_human_idmapping() -> None:
    descriptor = UniRef100MembershipsRecipe().descriptor

    assert descriptor.revision == "2"
    assert len(descriptor.inputs) == 1
    assert descriptor.inputs[0].kind is SnapshotKind.SOURCE
    assert descriptor.inputs[0].dataset == DatasetId("uniprot", "human_idmapping")


def test_projects_sorted_unique_uniref100_mappings(tmp_path: Path) -> None:
    recipe = UniRef100MembershipsRecipe(minimum_mappings=2)
    product = recipe.build(
        _inputs(
            recipe,
            tmp_path / "inputs",
            [
                ("Q00002", "UniRef100", "UniRef100_Q00002"),
                ("P00001", "GeneID", "1"),
                ("P00001", "UniRef100", "UniRef100_P00001"),
                ("P00001", "UniRef100", "UniRef100_P00001"),
            ],
        ),
        tmp_path / "output",
        NullProgressReporter(),
    )

    with product.files[0].local_path.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == [
            {"uniprot_id": "P00001", "uniref100_cluster_id": "UniRef100_P00001"},
            {"uniprot_id": "Q00002", "uniref100_cluster_id": "UniRef100_Q00002"},
        ]
    assert product.validation == {
        "source_rows": 4,
        "uniref100_rows": 3,
        "unique_accessions": 2,
        "unique_clusters": 2,
        "duplicate_rows": 1,
        "minimum_mappings": 2,
    }
    assert product.observations == ()


def test_rejects_conflicting_clusters_for_one_accession(tmp_path: Path) -> None:
    recipe = UniRef100MembershipsRecipe(minimum_mappings=1)

    with pytest.raises(InvalidDerivedBuildError, match="conflicting"):
        recipe.build(
            _inputs(
                recipe,
                tmp_path / "inputs",
                [
                    ("P00001", "UniRef100", "UniRef100_P00001"),
                    ("P00001", "UniRef100", "UniRef100_Q00002"),
                ],
            ),
            tmp_path / "output",
            NullProgressReporter(),
        )


def test_rejects_malformed_idmapping_rows(tmp_path: Path) -> None:
    recipe = UniRef100MembershipsRecipe(minimum_mappings=1)
    inputs = _inputs(
        recipe,
        tmp_path / "inputs",
        [("P00001", "UniRef100", "UniRef100_P00001")],
    )
    path = tmp_path / "inputs/human_idmapping/HUMAN_9606_idmapping.dat.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("not\tenough\n")

    with pytest.raises(InvalidDerivedBuildError, match="three non-empty fields"):
        recipe.build(inputs, tmp_path / "output", NullProgressReporter())


def test_rejects_implausibly_small_projection(tmp_path: Path) -> None:
    recipe = UniRef100MembershipsRecipe(minimum_mappings=2)

    with pytest.raises(InvalidDerivedBuildError, match="implausibly few"):
        recipe.build(
            _inputs(
                recipe,
                tmp_path / "inputs",
                [("P00001", "UniRef100", "UniRef100_P00001")],
            ),
            tmp_path / "output",
            NullProgressReporter(),
        )
