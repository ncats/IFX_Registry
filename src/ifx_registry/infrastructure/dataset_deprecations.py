"""Catalog lifecycle notices for datasets without installed source adapters.

These notices supplement the catalog; they never create catalog entries or
change immutable published manifests.
"""

from dataclasses import dataclass

from ifx_registry.application.use_cases.browse_catalog import CatalogKind
from ifx_registry.domain.models import DatasetId


@dataclass(frozen=True, slots=True)
class DatasetDeprecationNotice:
    message: str
    replacement: DatasetId
    replacement_kind: CatalogKind


def _target_graph_notice(file_name: str) -> DatasetDeprecationNotice:
    return DatasetDeprecationNotice(
        message=(
            "Deprecated for new integrations. Use the corresponding "
            f"{file_name} file from an exact ifx_harmonizers:targets release. "
            "Existing source version pins remain available."
        ),
        replacement=DatasetId("ifx_harmonizers", "targets"),
        replacement_kind=CatalogKind.DERIVED,
    )


DATASET_DEPRECATIONS = {
    (CatalogKind.EXTERNAL, DatasetId("drugcentral", "drug_database")):
        DatasetDeprecationNotice(
            message=(
                "Deprecated for new integrations. Use the DrugCentral export snapshot; "
                "SQL consumers must migrate to its file contract. "
                "Existing external version pins remain available."
            ),
            replacement=DatasetId("drugcentral", "drug_exports"),
            replacement_kind=CatalogKind.SOURCE,
        ),
    (CatalogKind.SOURCE, DatasetId("target_graph", "gene_ids")):
        _target_graph_notice("gene_ids.tsv"),
    (CatalogKind.SOURCE, DatasetId("target_graph", "protein_ids")):
        _target_graph_notice("protein_ids.tsv"),
    (CatalogKind.SOURCE, DatasetId("target_graph", "transcript_ids")):
        _target_graph_notice("transcript_ids.tsv"),
    (CatalogKind.SOURCE, DatasetId("target_graph", "uniprot_mapping")):
        _target_graph_notice("uniprot_mapping.csv"),
}
