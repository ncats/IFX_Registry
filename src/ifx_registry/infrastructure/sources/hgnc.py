"""Validated HGNC complete gene-set source dataset."""

from __future__ import annotations

import csv
import re

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

HGNC_COMPLETE_SET_FILE = "hgnc_complete_set.txt"
HGNC_COMPLETE_SET_URL = (
    "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/"
    f"{HGNC_COMPLETE_SET_FILE}"
)
HGNC_COMPLETE_SET_MINIMUM_ROWS = 40_000
HGNC_ID_PATTERN = re.compile(r"HGNC:[1-9][0-9]*")
HGNC_COMPLETE_SET_HEADER = (
    "hgnc_id",
    "symbol",
    "name",
    "locus_group",
    "locus_type",
    "status",
    "location",
    "alias_symbol",
    "alias_name",
    "prev_symbol",
    "prev_name",
    "gene_group",
    "gene_group_id",
    "date_approved_reserved",
    "date_symbol_changed",
    "date_name_changed",
    "date_modified",
    "entrez_id",
    "ensembl_gene_id",
    "vega_id",
    "ucsc_id",
    "ena",
    "refseq_accession",
    "ccds_id",
    "uniprot_ids",
    "pubmed_id",
    "mgd_id",
    "rgd_id",
    "lsdb",
    "cosmic",
    "omim_id",
    "mirbase",
    "homeodb",
    "snornabase",
    "bioparadigms_slc",
    "orphanet",
    "pseudogene.org",
    "horde_id",
    "merops",
    "imgt",
    "iuphar",
    "kznf_gene_catalog",
    "mamit-trnadb",
    "cd",
    "lncrnadb",
    "enzyme_id",
    "intermediate_filament_db",
    "rna_central_id",
    "lncipedia",
    "gtrnadb",
    "agr",
    "mane_select",
    "gencc",
)

HGNC_COMPLETE_SET_DEFINITION = LastModifiedSourceDefinition(
    DatasetId("hgnc", "complete_set"),
    (HttpFileSpec(HGNC_COMPLETE_SET_URL, HGNC_COMPLETE_SET_FILE),),
    "https://www.genenames.org/download/archive/",
    "Uses the complete-set file's Last-Modified date as its version.",
)


class HgncCompleteSetSource(LastModifiedHttpSource):
    """HGNC's complete gene set, preserved exactly as published upstream."""

    def __init__(
        self,
        http: HttpGateway,
        *,
        minimum_rows: int = HGNC_COMPLETE_SET_MINIMUM_ROWS,
    ):
        super().__init__(http, HGNC_COMPLETE_SET_DEFINITION)
        if minimum_rows <= 0:
            raise ValueError("minimum_rows must be positive")
        self._minimum_rows = minimum_rows

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        downloaded = require_download(downloads, HGNC_COMPLETE_SET_FILE)
        row_count = 0
        malformed_rows = 0
        invalid_ids = 0
        duplicate_ids = 0
        statuses: dict[str, int] = {}
        observed_ids: set[str] = set()
        try:
            with downloaded.resource.path.open(
                "r",
                encoding="utf-8",
                newline="",
            ) as handle:
                reader = csv.reader(handle, delimiter="\t")
                header = tuple(next(reader))
                if header != HGNC_COMPLETE_SET_HEADER:
                    raise SourceValidationError(
                        "HGNC complete-set header changed: " + "\t".join(header)
                    )
                status_index = header.index("status")
                for row in reader:
                    if not row:
                        continue
                    row_count += 1
                    if len(row) != len(HGNC_COMPLETE_SET_HEADER):
                        malformed_rows += 1
                        continue
                    hgnc_id = row[0]
                    if HGNC_ID_PATTERN.fullmatch(hgnc_id) is None:
                        invalid_ids += 1
                    elif hgnc_id in observed_ids:
                        duplicate_ids += 1
                    else:
                        observed_ids.add(hgnc_id)
                    status = row[status_index]
                    statuses[status] = statuses.get(status, 0) + 1
        except StopIteration as error:
            raise SourceValidationError("HGNC complete set is empty") from error
        except (OSError, UnicodeDecodeError, csv.Error) as error:
            raise SourceValidationError(
                f"Could not read HGNC complete-set TSV: {error}"
            ) from error

        if malformed_rows:
            raise SourceValidationError(
                f"HGNC complete set contains {malformed_rows} malformed rows"
            )
        if invalid_ids:
            raise SourceValidationError(
                f"HGNC complete set contains {invalid_ids} invalid HGNC IDs"
            )
        if duplicate_ids:
            raise SourceValidationError(
                f"HGNC complete set contains {duplicate_ids} duplicate HGNC IDs"
            )
        if row_count < self._minimum_rows:
            raise SourceValidationError(
                "HGNC complete-set row count is below the reviewed minimum: "
                f"{row_count} < {self._minimum_rows}"
            )

        return SourceValidationResult(
            version=version,
            metadata={
                "upstream_product": HGNC_COMPLETE_SET_FILE,
                "expected_header": list(HGNC_COMPLETE_SET_HEADER),
                "rows": row_count,
                "unique_hgnc_ids": len(observed_ids),
                "status_counts": dict(sorted(statuses.items())),
                "minimum_rows": self._minimum_rows,
            },
        )
