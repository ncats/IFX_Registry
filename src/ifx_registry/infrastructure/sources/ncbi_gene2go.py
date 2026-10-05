"""Byte-faithful NCBI gene2go export with streaming validation."""

from __future__ import annotations

import gzip
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

GENE2GO_URL = "https://ftp.ncbi.nlm.nih.gov/gene/DATA/gene2go.gz"
GENE2GO_HEADER = (
    "#tax_id",
    "GeneID",
    "GO_ID",
    "Evidence",
    "Qualifier",
    "GO_term",
    "PubMed",
    "Category",
)
GENE2GO_DEFINITION = LastModifiedSourceDefinition(
    DatasetId("ncbi", "gene2go"),
    (HttpFileSpec(GENE2GO_URL, "gene2go.gz"),),
    "https://www.ncbi.nlm.nih.gov/gene/",
    "Uses the NCBI gene2go Last-Modified date; discovery downloads only HTTP headers.",
)


class NcbiGene2GoSource(LastModifiedHttpSource):
    """Preserve all species and assertions; consumers select their own mappings."""

    def __init__(
        self,
        http: HttpGateway,
        *,
        minimum_rows: int = 1_000_000,
        minimum_human_rows: int = 100_000,
    ):
        super().__init__(http, GENE2GO_DEFINITION)
        if minimum_rows <= 0 or minimum_human_rows <= 0:
            raise ValueError("Minimum gene2go row counts must be positive")
        self._minimum_rows = minimum_rows
        self._minimum_human_rows = minimum_human_rows

    def validate_confirmation(
        self, version: SourceVersion, confirmed_version: SourceVersion
    ) -> None:
        super().validate_confirmation(version, confirmed_version)
        if version.evidence["files"] != confirmed_version.evidence["files"]:
            raise SourceValidationError("NCBI gene2go changed during acquisition")

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        downloaded = require_download(downloads, "gene2go.gz")
        expected = version.evidence["files"][0]["last_modified"]
        if downloaded.resource.metadata.header("last-modified") != expected:
            raise SourceValidationError("NCBI gene2go Last-Modified changed during download")
        rows = human_rows = 0
        taxon_counts: dict[str, int] = {}
        categories: dict[str, int] = {}
        go_pattern = re.compile(r"GO:\d{7}")
        try:
            with gzip.open(downloaded.resource.path, "rt", encoding="utf-8") as handle:
                header = tuple(handle.readline().rstrip("\r\n").split("\t"))
                if header != GENE2GO_HEADER:
                    raise SourceValidationError("NCBI gene2go header changed or file is empty")
                for line in handle:
                    if not line.strip():
                        continue
                    fields = line.rstrip("\r\n").split("\t")
                    if (
                        len(fields) != 8
                        or not fields[0].isdigit()
                        or not fields[1].isdigit()
                        or not go_pattern.fullmatch(fields[2])
                        or not fields[3]
                        or not fields[5]
                        or fields[7] not in ("Function", "Process", "Component")
                    ):
                        raise SourceValidationError(
                            f"Malformed NCBI gene2go record at row {rows + 1}"
                        )
                    rows += 1
                    human_rows += fields[0] == "9606"
                    taxon_counts[fields[0]] = taxon_counts.get(fields[0], 0) + 1
                    categories[fields[7]] = categories.get(fields[7], 0) + 1
        except (OSError, EOFError, UnicodeError) as error:
            raise SourceValidationError(
                f"Could not read complete NCBI gene2go gzip: {error}"
            ) from error
        if rows < self._minimum_rows or human_rows < self._minimum_human_rows:
            raise SourceValidationError(
                f"NCBI gene2go below minimum coverage: {rows} total, {human_rows} human rows"
            )
        return SourceValidationResult(
            version,
            {
                "upstream_product": "gene2go.gz",
                "rows": rows,
                "human_rows": human_rows,
                "taxon_counts": dict(sorted(taxon_counts.items())),
                "category_counts": categories,
                "expected_header": list(GENE2GO_HEADER),
                "minimum_rows": self._minimum_rows,
                "minimum_human_rows": self._minimum_human_rows,
                "scope": "All upstream taxa; no filtering, deduplication, or ID remapping",
            },
        )
