"""Tests for the Registry-owned human NCBI mapping projection."""

from __future__ import annotations

import gzip
from dataclasses import replace
from pathlib import Path

import pytest

from ifx_registry.application.derived_build_models import MaterializedRecipeInput
from ifx_registry.application.progress import NullProgressReporter
from ifx_registry.domain.catalog import RegisteredSnapshotRef
from ifx_registry.domain.errors import InvalidDerivedBuildError
from ifx_registry.domain.models import SnapshotRef
from ifx_registry.infrastructure.recipes.ncbi_gene_mappings import (
    NcbiHumanGeneIdentifierMappingsRecipe,
)
from ifx_registry.infrastructure.sources.ncbi_gene_mappings import (
    NCBI_GENE_MAPPING_FILES,
)


def _small_files():
    return tuple(
        replace(definition, minimum_human_rows=1)
        for definition in NCBI_GENE_MAPPING_FILES
    )


def _row(definition, taxon: str, marker: str, ending: bytes = b"\n") -> bytes:
    values = [f"{marker}-{index}" for index in range(len(definition.header))]
    values[definition.taxon_index] = taxon
    return "\t".join(values).encode() + ending


def _write_inputs(directory: Path, files) -> dict[str, bytes]:
    directory.mkdir()
    expected: dict[str, bytes] = {}
    for definition in files:
        header = "\t".join(definition.header).encode() + b"\r\n"
        human_one = _row(definition, "9606", "human-one", b"\r\n")
        mouse = _row(definition, "10090", "mouse")
        human_two = _row(definition, "9606", "human-two")
        payload = header + human_one + mouse + human_two
        (directory / definition.file.name).write_bytes(gzip.compress(payload))
        expected[definition.file.name] = header + human_one + human_two
    return expected


def _recipe_input(recipe, input_dir: Path) -> dict[str, MaterializedRecipeInput]:
    slot = recipe.descriptor.inputs[0]
    reference = SnapshotRef.source("ncbi:gene_identifier_mappings:2026-09-15")
    registered = RegisteredSnapshotRef(reference, "s3://registry/manifest.yaml", "a" * 64)
    return {slot.name: MaterializedRecipeInput(slot, registered, input_dir)}


def test_recipe_filters_all_files_and_preserves_selected_row_bytes(tmp_path: Path) -> None:
    files = _small_files()
    input_dir = tmp_path / "input"
    expected = _write_inputs(input_dir, files)
    recipe = NcbiHumanGeneIdentifierMappingsRecipe(files=files)

    product = recipe.build(
        _recipe_input(recipe, input_dir),
        tmp_path / "output",
        NullProgressReporter(),
    )

    assert [str(item.relative_path) for item in product.files] == [
        definition.file.name for definition in files
    ]
    for item in product.files:
        assert gzip.decompress(item.local_path.read_bytes()) == expected[str(item.relative_path)]
    assert product.validation["taxon_id"] == 9606
    assert product.validation["human_only"] is True
    assert product.validation["total_source_rows"] == 9
    assert product.validation["total_rows"] == 6
    for definition in files:
        assert product.validation["files"][definition.file.name]["source_rows"] == 3
        assert product.validation["files"][definition.file.name]["output_rows"] == 2
    assert product.version_date.isoformat() == "2026-09-15"


def test_recipe_outputs_are_byte_deterministic(tmp_path: Path) -> None:
    files = _small_files()
    input_dir = tmp_path / "input"
    _write_inputs(input_dir, files)
    recipe = NcbiHumanGeneIdentifierMappingsRecipe(files=files)

    first = recipe.build(
        _recipe_input(recipe, input_dir),
        tmp_path / "first",
        NullProgressReporter(),
    )
    second = recipe.build(
        _recipe_input(recipe, input_dir),
        tmp_path / "second",
        NullProgressReporter(),
    )

    assert [item.local_path.read_bytes() for item in first.files] == [
        item.local_path.read_bytes() for item in second.files
    ]


def test_recipe_rejects_taxonomy_in_the_wrong_field(tmp_path: Path) -> None:
    files = _small_files()
    input_dir = tmp_path / "input"
    _write_inputs(input_dir, files)
    collaboration = files[-1]
    values = ["9606", "P12345", "invalid", "9606", "method"]
    payload = "\t".join(collaboration.header).encode() + b"\n"
    payload += "\t".join(values).encode() + b"\n"
    (input_dir / collaboration.file.name).write_bytes(gzip.compress(payload))
    recipe = NcbiHumanGeneIdentifierMappingsRecipe(files=files)

    with pytest.raises(InvalidDerivedBuildError, match="invalid taxonomy ID"):
        recipe.build(
            _recipe_input(recipe, input_dir),
            tmp_path / "output",
            NullProgressReporter(),
        )
