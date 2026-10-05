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
}
