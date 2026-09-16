"""Validated NCBI gene identifier mapping bundle."""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path

from ifx_registry.application.contracts import VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
    require_download,
)
from ifx_registry.infrastructure.sources.version_strategies import SourceVersionStrategy


@dataclass(frozen=True, slots=True)
class NcbiMappingFile:
    file: HttpFileSpec
    header: tuple[str, ...]
    taxon_index: int
    minimum_human_rows: int


NCBI_GENE_MAPPING_FILES = (
    NcbiMappingFile(
        HttpFileSpec(
            "https://ftp.ncbi.nih.gov/gene/DATA/gene2refseq.gz",
            "gene2refseq.gz",
        ),
        (
            "#tax_id", "GeneID", "status", "RNA_nucleotide_accession.version",
            "RNA_nucleotide_gi", "protein_accession.version", "protein_gi",
            "genomic_nucleotide_accession.version", "genomic_nucleotide_gi",
            "start_position_on_the_genomic_accession",
            "end_position_on_the_genomic_accession", "orientation", "assembly",
            "mature_peptide_accession.version", "mature_peptide_gi", "Symbol",
        ),
        0,
        700_000,
    ),
    NcbiMappingFile(
        HttpFileSpec(
            "https://ftp.ncbi.nih.gov/gene/DATA/gene2ensembl.gz",
            "gene2ensembl.gz",
        ),
        (
            "#tax_id", "GeneID", "Ensembl_gene_identifier",
            "RNA_nucleotide_accession.version", "Ensembl_rna_identifier",
            "protein_accession.version", "Ensembl_protein_identifier",
        ),
        0,
        80_000,
    ),
    NcbiMappingFile(
        HttpFileSpec(
            "https://ftp.ncbi.nih.gov/gene/DATA/gene_refseq_uniprotkb_collab.gz",
            "gene_refseq_uniprotkb_collab.gz",
        ),
        (
            "#NCBI_protein_accession", "UniProtKB_protein_accession",
            "NCBI_tax_id", "UniProtKB_tax_id", "method",
        ),
        2,
        300_000,
    ),
)


@dataclass(frozen=True, slots=True)
class CoherentLastModifiedStrategy(SourceVersionStrategy):
    files: tuple[NcbiMappingFile, ...]
    description: str = (
        "Requires all three NCBI mapping products to have the same "
        "Last-Modified date."
    )

    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return tuple(item.file.url for item in self.files)

    def discover(
        self,
        http: HttpGateway,
        request: VersionProbeRequest,
    ) -> SourceVersion:
        observations: list[dict[str, str]] = []
        dates: set[date] = set()
        for item in self.files:
            metadata = http.head(
                item.file.url,
                timeout=request.timeout.total_seconds(),
            )
            raw = metadata.header("Last-Modified")
            if not raw:
                raise SourceValidationError(
                    f"NCBI mapping file {item.file.name} has no Last-Modified header"
                )
            try:
                observed_date = parsedate_to_datetime(raw).date()
            except (TypeError, ValueError) as error:
                raise SourceValidationError(
                    f"Could not parse Last-Modified header {raw!r}"
                ) from error
            dates.add(observed_date)
            observations.append(
                {
                    "file": item.file.name,
                    "url": metadata.final_url,
                    "last_modified": raw,
                    "date": observed_date.isoformat(),
                }
            )
        if len(dates) != 1:
            raise SourceValidationError(
                "NCBI gene mapping files do not share one release date: "
                + ", ".join(
                    f"{item['file']}={item['date']}" for item in observations
                )
            )
        version_date = next(iter(dates))
        return SourceVersion(
            version_date.isoformat(),
            version_date=version_date,
            evidence={
                "method": "coherent_multi_file_last_modified",
                "files": observations,
            },
        )


class NcbiGeneIdentifierMappingsSource(HttpSnapshotSource):
    """Three release-coherent, byte-faithful NCBI identifier mapping files."""

    _dataset = DatasetId("ncbi", "gene_identifier_mappings")
    def __init__(
        self,
        http: HttpGateway,
        *,
        files: tuple[NcbiMappingFile, ...] = NCBI_GENE_MAPPING_FILES,
    ):
        super().__init__(http)
        if not files:
            raise ValueError("NCBI mapping source must contain at least one file")
        self._files = files
        self._version_strategy = CoherentLastModifiedStrategy(files)

    @property
    def dataset(self) -> DatasetId:
        return self._dataset

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return tuple(item.file for item in self._files)

    @property
    def homepage(self) -> str:
        return "https://www.ncbi.nlm.nih.gov/gene/"

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return self._version_strategy

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        profiles: dict[str, dict[str, object]] = {}
        for definition in self._files:
            download = require_download(downloads, definition.file.name)
            profile = profile_mapping_gzip(download.resource.path, definition)
            human_rows = profile["human_rows"]
            if not isinstance(human_rows, int):
                raise SourceValidationError(
                    f"{definition.file.name} validation produced an invalid row count"
                )
            if human_rows < definition.minimum_human_rows:
                raise SourceValidationError(
                    f"{definition.file.name} human row count is below the reviewed "
                    f"minimum: {human_rows} < {definition.minimum_human_rows}"
                )
            profiles[definition.file.name] = {
                **profile,
                "minimum_human_rows": definition.minimum_human_rows,
            }
        return SourceValidationResult(
            version,
            metadata={"taxon_id": 9606, "files": profiles},
        )


def profile_mapping_gzip(
    path: Path,
    definition: NcbiMappingFile,
    *,
    require_human_only: bool = False,
) -> dict[str, object]:
    rows = 0
    human_rows = 0
    malformed_rows = 0
    invalid_taxon_rows = 0
    nonhuman_rows = 0
    expected_tabs = len(definition.header) - 1
    try:
        with gzip.open(path, "rb") as handle:
            raw_header = next(handle).rstrip(b"\r\n")
            header = tuple(value.decode("utf-8") for value in raw_header.split(b"\t"))
            if header != definition.header:
                raise SourceValidationError(
                    f"{definition.file.name} header changed: " + "\t".join(header)
                )
            for line in handle:
                stripped = line.rstrip(b"\r\n")
                if not stripped:
                    continue
                rows += 1
                if stripped.count(b"\t") != expected_tabs:
                    malformed_rows += 1
                    continue
                fields = stripped.split(b"\t", definition.taxon_index + 1)
                taxon = fields[definition.taxon_index]
                if not taxon.isdigit():
                    invalid_taxon_rows += 1
                elif taxon == b"9606":
                    human_rows += 1
                else:
                    nonhuman_rows += 1
    except StopIteration as error:
        raise SourceValidationError(f"{definition.file.name} is empty") from error
    except (OSError, UnicodeDecodeError) as error:
        raise SourceValidationError(
            f"Could not read {definition.file.name}: {error}"
        ) from error
    if malformed_rows:
        raise SourceValidationError(
            f"{definition.file.name} contains {malformed_rows} malformed rows"
        )
    if invalid_taxon_rows:
        raise SourceValidationError(
            f"{definition.file.name} contains {invalid_taxon_rows} invalid taxonomy IDs"
        )
    if require_human_only and nonhuman_rows:
        raise SourceValidationError(
            f"{definition.file.name} contains {nonhuman_rows} non-human rows"
        )
    return {
        "header": list(definition.header),
        "columns": len(definition.header),
        "rows": rows,
        "human_rows": human_rows,
        "nonhuman_rows": nonhuman_rows,
        "gzip_valid": True,
    }
