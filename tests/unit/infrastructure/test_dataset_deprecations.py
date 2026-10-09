"""Catalog-only deprecations retain exact source pins and name their replacements."""

import pytest

from ifx_registry.application.use_cases.browse_catalog import CatalogKind
from ifx_registry.domain.models import DatasetId
from ifx_registry.infrastructure.dataset_deprecations import DATASET_DEPRECATIONS


@pytest.mark.parametrize(
    ("dataset", "file_name"),
    (
        ("gene_ids", "gene_ids.tsv"),
        ("protein_ids", "protein_ids.tsv"),
        ("transcript_ids", "transcript_ids.tsv"),
        ("uniprot_mapping", "uniprot_mapping.csv"),
    ),
)
def test_target_graph_deprecation_points_to_harmonizer_file(
    dataset: str, file_name: str
) -> None:
    notice = DATASET_DEPRECATIONS[
        (CatalogKind.SOURCE, DatasetId("target_graph", dataset))
    ]
    assert notice.replacement == DatasetId("ifx_harmonizers", "targets")
    assert notice.replacement_kind is CatalogKind.DERIVED
    assert file_name in notice.message
    assert "Existing source version pins remain available" in notice.message
