"""NCBI's standalone OMIM-to-gene and MedGen association export."""

from __future__ import annotations

from pathlib import Path

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


MIM2GENE_MEDGEN_URL = "https://ftp.ncbi.nlm.nih.gov/gene/DATA/mim2gene_medgen"
MIM2GENE_MEDGEN_NAME = "mim2gene_medgen.tsv"
MIM2GENE_MEDGEN_HEADER = (
    "#MIM number", "GeneID", "type", "Source", "MedGenCUI", "Comment",
)
MIM2GENE_MEDGEN_DEFINITION = LastModifiedSourceDefinition(
    DatasetId("ncbi", "mim2gene_medgen"),
    (HttpFileSpec(MIM2GENE_MEDGEN_URL, MIM2GENE_MEDGEN_NAME),),
    "https://www.ncbi.nlm.nih.gov/medgen/",
    "Uses the NCBI mim2gene_medgen Last-Modified date as its version.",
)


def _validate_file(path: Path) -> int:
    try:
        with path.open(encoding="utf-8") as handle:
            header = tuple(handle.readline().rstrip("\r\n").split("\t"))
            if header != MIM2GENE_MEDGEN_HEADER:
                raise SourceValidationError("NCBI mim2gene_medgen header changed")
            rows = 0
            for raw in handle:
                fields = raw.rstrip("\r\n").split("\t")
                if (
                    len(fields) != len(MIM2GENE_MEDGEN_HEADER)
                    or not fields[0].isdigit()
                    or fields[2] not in {"gene", "phenotype"}
                ):
                    raise SourceValidationError(
                        f"Malformed NCBI mim2gene_medgen record at row {rows + 1}"
                    )
                rows += 1
    except (OSError, UnicodeError) as error:
        raise SourceValidationError(f"Could not read NCBI mim2gene_medgen file: {error}") from error
    if rows < 10_000:
        raise SourceValidationError(
            f"NCBI mim2gene_medgen has too few records: {rows} < 10000"
        )
    return rows


class NcbiMim2geneMedgenSource(LastModifiedHttpSource):
    """Preserve NCBI's complete raw gene and phenotype association file."""

    def __init__(self, http: HttpGateway):
        super().__init__(http, MIM2GENE_MEDGEN_DEFINITION)

    def validate_downloads(
        self, version: SourceVersion, downloads: tuple[DownloadedSourceFile, ...]
    ) -> SourceValidationResult:
        downloaded = require_download(downloads, MIM2GENE_MEDGEN_NAME)
        expected = version.evidence["files"][0]["last_modified"]
        actual = downloaded.resource.metadata.header("Last-Modified")
        if actual != expected:
            raise SourceValidationError(
                "NCBI mim2gene_medgen Last-Modified changed during download"
            )
        rows = _validate_file(downloaded.resource.path)
        return SourceValidationResult(version, metadata={"rows": rows})

    def validate_confirmation(
        self, version: SourceVersion, confirmed_version: SourceVersion
    ) -> None:
        super().validate_confirmation(version, confirmed_version)
        if version.evidence["files"] != confirmed_version.evidence["files"]:
            raise SourceValidationError("NCBI mim2gene_medgen changed during acquisition")
