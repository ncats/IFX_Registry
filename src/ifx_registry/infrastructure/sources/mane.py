"""NCBI's independently versioned human MANE summary, preserved byte for byte."""

from __future__ import annotations

import gzip
import re
from collections import Counter
from dataclasses import replace

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
from ifx_registry.infrastructure.sources.version_strategies import (
    ParsedTextVersionStrategy,
    SourceVersionStrategy,
)

MANE_BASE = "https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human"
MANE_VERSION_URL = f"{MANE_BASE}/current/README_versions.txt"
MANE_FILE = "mane_summary.tsv.gz"
MANE_HEADER = (
    "#NCBI_GeneID",
    "Ensembl_Gene",
    "HGNC_ID",
    "symbol",
    "name",
    "RefSeq_nuc",
    "RefSeq_prot",
    "Ensembl_nuc",
    "Ensembl_prot",
    "MANE_status",
    "GRCh38_chr",
    "chr_start",
    "chr_end",
    "chr_strand",
)


def parse_mane_version(text: str) -> SourceVersion:
    entries = {}
    for line in text.splitlines():
        key, separator, value = line.partition("\t")
        if not separator:
            continue
        if key.strip() in entries:
            raise SourceValidationError("Duplicate MANE version metadata key")
        entries[key.strip()] = value.strip()
    version = entries.get("MANE Version", "")
    ensembl = entries.get("Ensembl Release", "")
    refseq = entries.get("NCBI RefSeq Annotation Release", "")
    if not re.fullmatch(r"\d+(?:\.\d+)+", version) or not ensembl.isdigit() or not refseq:
        raise SourceValidationError("Invalid or incomplete MANE README_versions.txt")
    return SourceVersion(
        version,
        evidence={
            "mane_release": version,
            "ensembl_release": ensembl,
            "refseq_annotation_release": refseq,
        },
    )


def mane_summary_url(version: str) -> str:
    return f"{MANE_BASE}/release_{version}/MANE.GRCh38.v{version}.summary.txt.gz"


class ManeHumanSummarySource(HttpSnapshotSource):
    def __init__(self, http: HttpGateway, *, minimum_rows: int = 15000):
        super().__init__(http)
        if minimum_rows <= 0:
            raise ValueError("minimum_rows must be positive")
        self._minimum_rows = minimum_rows

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("ncbi", "mane_human_summary")

    @property
    def homepage(self) -> str:
        return "https://www.ncbi.nlm.nih.gov/refseq/MANE/"

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return (HttpFileSpec(mane_summary_url("{release}"), MANE_FILE),)

    def file_specs_for(self, version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        if not re.fullmatch(r"\d+(?:\.\d+)+", version.value):
            raise SourceValidationError("MANE requires a numeric dotted release")
        return (HttpFileSpec(mane_summary_url(version.value), MANE_FILE),)

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return ParsedTextVersionStrategy(
            MANE_VERSION_URL,
            parse_mane_version,
            "mane_release_readme",
            "Reads the current MANE release metadata and acquires its GRCh38 summary "
            "from the numbered release directory, independently of Ensembl FTP.",
        )

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        version = super().discover_latest(request)
        metadata = self._http.head(
            mane_summary_url(version.value),
            timeout=request.timeout.total_seconds(),
        )
        raw_size = metadata.header("Content-Length")
        modified = metadata.header("Last-Modified")
        if not raw_size or not raw_size.isdigit() or int(raw_size) <= 0 or not modified:
            raise SourceValidationError("MANE summary lacks size or Last-Modified metadata")
        return replace(
            version,
            evidence={
                **version.evidence,
                "summary_size_bytes": int(raw_size),
                "summary_last_modified": modified,
            },
        )

    def validate_confirmation(self, version: SourceVersion, confirmed: SourceVersion) -> None:
        super().validate_confirmation(version, confirmed)
        if version.evidence != confirmed.evidence:
            raise SourceValidationError("MANE release metadata changed during acquisition")

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        downloaded = require_download(downloads, MANE_FILE)
        if downloaded.resource.path.stat().st_size != version.evidence["summary_size_bytes"]:
            raise SourceValidationError("MANE summary size differs from the release probe")
        if (
            downloaded.resource.metadata.header("Last-Modified")
            != version.evidence["summary_last_modified"]
        ):
            raise SourceValidationError("MANE summary changed before download")
        statuses: Counter[str] = Counter()
        genes = set()
        missing_optional: Counter[str] = Counter()
        try:
            with gzip.open(downloaded.resource.path, "rt", encoding="utf-8") as handle:
                header = tuple(handle.readline().rstrip("\r\n").split("\t"))
                if header != MANE_HEADER:
                    raise SourceValidationError("Unexpected MANE summary header")
                for line_number, line in enumerate(handle, 2):
                    fields = line.rstrip("\r\n").split("\t")
                    if len(fields) != len(MANE_HEADER):
                        raise SourceValidationError(f"Malformed MANE row {line_number}")
                    row = dict(zip(MANE_HEADER, fields, strict=True))
                    patterns = {
                        "#NCBI_GeneID": r"GeneID:\d+",
                        "Ensembl_Gene": r"ENSG\d+\.\d+",
                        "HGNC_ID": r"HGNC:\d+",
                        "RefSeq_nuc": r"N[MR]_\d+\.\d+",
                        "RefSeq_prot": r"NP_\d+\.\d+",
                        "Ensembl_nuc": r"ENST\d+\.\d+",
                        "Ensembl_prot": r"ENSP\d+\.\d+",
                        "GRCh38_chr": r"N[CTW]_\d+\.\d+",
                    }
                    for column, pattern in patterns.items():
                        if column in {"HGNC_ID", "RefSeq_prot", "Ensembl_prot"} and not row[column]:
                            missing_optional[column] += 1
                            continue
                        if not re.fullmatch(pattern, row[column]):
                            raise SourceValidationError(
                                f"Invalid MANE {column} at row {line_number}"
                            )
                    status = row["MANE_status"]
                    if status not in {"MANE Select", "MANE Plus Clinical"}:
                        raise SourceValidationError(f"Unexpected MANE status: {status}")
                    if (
                        not row["chr_start"].isdigit()
                        or not row["chr_end"].isdigit()
                        or not 0 < int(row["chr_start"]) <= int(row["chr_end"])
                        or row["chr_strand"] not in {"+", "-"}
                    ):
                        raise SourceValidationError(
                            f"Invalid MANE coordinates at row {line_number}"
                        )
                    genes.add(row["#NCBI_GeneID"])
                    statuses[status] += 1
        except (OSError, EOFError, UnicodeError) as error:
            raise SourceValidationError(f"Cannot read MANE summary gzip: {error}") from error
        if sum(statuses.values()) < self._minimum_rows or not statuses["MANE Select"]:
            raise SourceValidationError("MANE summary lacks expected row or MANE Select coverage")
        return SourceValidationResult(
            version,
            {
                "assembly": "GRCh38",
                "species": "Homo sapiens",
                "validation": {
                    "rows": sum(statuses.values()),
                    "genes": len(genes),
                    "status_counts": dict(statuses),
                    "gzip_valid": True,
                    "columns": list(MANE_HEADER),
                    "size_verified": True,
                "missing_optional_fields": dict(missing_optional),
                },
            },
        )
