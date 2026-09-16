"""Validated NCBI Gene source datasets."""

from __future__ import annotations

import csv
import gzip

from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    SourceValidationResult,
    require_download,
)
from ifx_registry.infrastructure.sources.last_modified import (
    LastModifiedHttpSource,
    LastModifiedSourceDefinition,
)

NCBI_HUMAN_GENE_INFO_FILE = "Homo_sapiens.gene_info.gz"
NCBI_HUMAN_GENE_INFO_URL = (
    "https://ftp.ncbi.nih.gov/gene/DATA/GENE_INFO/Mammalia/"
    f"{NCBI_HUMAN_GENE_INFO_FILE}"
)
NCBI_HUMAN_GENE_INFO_MINIMUM_ROWS = 150_000
NCBI_HUMAN_GENE_INFO_HEADER = (
    "#tax_id",
    "GeneID",
    "Symbol",
    "LocusTag",
    "Synonyms",
    "dbXrefs",
    "chromosome",
    "map_location",
    "description",
    "type_of_gene",
    "Symbol_from_nomenclature_authority",
    "Full_name_from_nomenclature_authority",
    "Nomenclature_status",
    "Other_designations",
    "Modification_date",
    "Feature_type",
)

NCBI_HUMAN_GENE_INFO_DEFINITION = LastModifiedSourceDefinition(
    DatasetId("ncbi", "human_gene_info"),
    (HttpFileSpec(NCBI_HUMAN_GENE_INFO_URL, NCBI_HUMAN_GENE_INFO_FILE),),
    "https://www.ncbi.nlm.nih.gov/gene/",
    "Uses the Homo sapiens gene_info file's Last-Modified date as its version.",
)


class NcbiHumanGeneInfoSource(LastModifiedHttpSource):
    """NCBI's complete Homo sapiens gene_info product, preserved as published."""

    def __init__(
        self,
        http: HttpGateway,
        *,
        minimum_human_rows: int = NCBI_HUMAN_GENE_INFO_MINIMUM_ROWS,
    ):
        super().__init__(http, NCBI_HUMAN_GENE_INFO_DEFINITION)
        if minimum_human_rows <= 0:
            raise ValueError("minimum_human_rows must be positive")
        self._minimum_human_rows = minimum_human_rows

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        downloaded = require_download(downloads, NCBI_HUMAN_GENE_INFO_FILE)
        row_count = 0
        malformed_rows = 0
        taxon_counts: dict[str, int] = {}
        try:
            with gzip.open(
                downloaded.resource.path,
                "rt",
                encoding="utf-8",
                newline="",
            ) as handle:
                reader = csv.reader(handle, delimiter="\t")
                header = tuple(next(reader))
                if header != NCBI_HUMAN_GENE_INFO_HEADER:
                    raise SourceValidationError(
                        "NCBI human gene_info header changed: " + "\t".join(header)
                    )
                for row in reader:
                    if not row:
                        continue
                    row_count += 1
                    if len(row) != len(NCBI_HUMAN_GENE_INFO_HEADER):
                        malformed_rows += 1
                        continue
                    taxon_counts[row[0]] = taxon_counts.get(row[0], 0) + 1
        except StopIteration as error:
            raise SourceValidationError("NCBI human gene_info is empty") from error
        except (OSError, UnicodeDecodeError, csv.Error) as error:
            raise SourceValidationError(
                f"Could not read NCBI human gene_info gzip: {error}"
            ) from error

        if malformed_rows:
            raise SourceValidationError(
                f"NCBI human gene_info contains {malformed_rows} malformed rows"
            )
        human_rows = taxon_counts.get("9606", 0)
        if row_count < self._minimum_human_rows:
            raise SourceValidationError(
                "NCBI human gene_info row count is below the reviewed minimum: "
                f"{row_count} < {self._minimum_human_rows}"
            )
        if human_rows < self._minimum_human_rows:
            raise SourceValidationError(
                "NCBI human gene_info human row count is below the reviewed minimum: "
                f"{human_rows} < {self._minimum_human_rows}"
            )

        return SourceValidationResult(
            version=version,
            metadata={
                "upstream_product": NCBI_HUMAN_GENE_INFO_FILE,
                "expected_header": list(NCBI_HUMAN_GENE_INFO_HEADER),
                "rows": row_count,
                "taxon_counts": dict(sorted(taxon_counts.items())),
                "primary_taxon_id": 9606,
                "minimum_human_rows": self._minimum_human_rows,
                "gzip_valid": True,
            },
        )
