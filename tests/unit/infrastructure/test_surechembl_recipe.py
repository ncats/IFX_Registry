"""Tests for the Registry-owned SureChEMBL derived recipe."""

from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ifx_registry.application.derived_build_models import MaterializedRecipeInput
from ifx_registry.application.progress import NullProgressReporter
from ifx_registry.domain.catalog import RegisteredSnapshotRef
from ifx_registry.domain.errors import InvalidDerivedBuildError
from ifx_registry.domain.models import SnapshotRef
from ifx_registry.infrastructure.recipes.surechembl import (
    SurechemblPatentFamilyMentionsRecipe,
    _load_entity_targets,
)


@pytest.mark.parametrize("entity_type_column", ["type_id", "entity_type_id"])
def test_recipe_aggregates_exact_source_into_patent_family_sets(
    tmp_path, entity_type_column: str
) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    pq.write_table(
        pa.table(
            {
                "id": [1, 2],
                entity_type_column: [1, 1],
                "resolved_form": ["P12345", "HGNC:7"],
            }
        ),
        input_dir / "biomedical_entities.parquet",
    )
    pq.write_table(
        pa.table({"patent_id": [10, 11, 12], "entity_id": [1, 1, 2]}),
        input_dir / "biomedical_locations.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "id": [10, 11, 12],
                "publication_date": pa.array(
                    [date(2020, 1, 1), date(2020, 2, 1), date(2027, 1, 1)],
                    type=pa.date32(),
                ),
                "family_id": [100, 100, 200],
            }
        ),
        input_dir / "patents.parquet",
    )
    recipe = SurechemblPatentFamilyMentionsRecipe()
    slot = recipe.descriptor.inputs[0]
    reference = SnapshotRef.source("surechembl:patent_discovery:2026-09-08")
    registered = RegisteredSnapshotRef(reference, "s3://registry/manifest.yaml", "a" * 64)

    product = recipe.build(
        {slot.name: MaterializedRecipeInput(slot, registered, input_dir)},
        tmp_path / "output",
        NullProgressReporter(),
    )

    output = pq.read_table(product.files[0].local_path).to_pylist()
    assert output == [
        {
            "protein_id": "UniProtKB:P12345",
            "patent_family_mentions": ["2020:100"],
            "patent_identifier_sources": ["UniProtKB"],
        }
    ]
    assert product.validation == {
        "row_count": 1,
        "entity_target_count": 2,
        "relevant_patent_count": 3,
        "patent_metadata_count": 2,
        "min_publication_year": 1950,
        "max_publication_year": 2026,
        "entity_type_column": entity_type_column,
    }


def test_recipe_rejects_unknown_entity_type_column(tmp_path) -> None:
    path = tmp_path / "biomedical_entities.parquet"
    pq.write_table(
        pa.table(
            {
                "id": [1],
                "unexpected_type": [1],
                "resolved_form": ["P12345"],
            }
        ),
        path,
    )

    with pytest.raises(
        InvalidDerivedBuildError,
        match=r"must contain type_id \(current\) or entity_type_id \(legacy\)",
    ):
        _load_entity_targets(path)
