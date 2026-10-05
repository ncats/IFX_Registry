"""Byte-faithful human Ensembl FTP files, excluding the separate NCBI MANE source."""

from __future__ import annotations

import gzip
import hashlib
import re
from pathlib import Path
from typing import Any

from ifx_registry.application.contracts import VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.ensembl import ENSEMBL_RELEASE_URL
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
    require_download,
)
from ifx_registry.infrastructure.sources.version_strategies import SourceVersionStrategy

FTP_BASE = "https://ftp.ensembl.org/pub"
XREF_HEADER = (
    "gene_stable_id",
    "transcript_stable_id",
    "protein_stable_id",
    "xref",
    "db_name",
    "info_type",
    "source_identity",
    "xref_identity",
    "linkage_type",
)
MYSQL_COLUMNS = {"gene": 16, "object_xref": 6, "xref": 8, "external_synonym": 2}
MINIMUM_ROWS = {
    "gff3": 70000,
    "entrez": 100000,
    "refseq": 100000,
    "uniprot": 50000,
    "gene": 70000,
    "object_xref": 1000000,
    "xref": 1000000,
    "external_synonym": 10000,
}


def ensembl_ftp_files(release: str) -> tuple[HttpFileSpec, ...]:
    """Use stable consumer filenames and exact release-specific upstream URLs."""
    base = f"{FTP_BASE}/release-{release}"
    prefix = f"Homo_sapiens.GRCh38.{release}"
    core = f"homo_sapiens_core_{release}_38"
    files = [
        HttpFileSpec(
            f"{base}/gff3/homo_sapiens/{prefix}.chr_patch_hapl_scaff.gff3.gz",
            "ensembl_homo_sapiens.gff3.gz",
        )
    ]
    files.extend(
        HttpFileSpec(
            f"{base}/tsv/homo_sapiens/{prefix}.{kind}.tsv.gz",
            f"ensembl_{kind}.tsv.gz",
        )
        for kind in ("entrez", "refseq", "uniprot")
    )
    files.extend(
        HttpFileSpec(
            f"{base}/mysql/{core}/{table}.txt.gz",
            f"ensembl_mysql_{table}.txt.gz",
        )
        for table in MYSQL_COLUMNS
    )
    files.append(
        HttpFileSpec(
            f"{base}/mysql/{core}/{core}.sql.gz",
            "ensembl_mysql_schema.sql.gz",
        )
    )
    files.extend(
        HttpFileSpec(f"{base}/{directory}/CHECKSUMS", f"{kind}_CHECKSUMS")
        for kind, directory in (
            ("gff3", "gff3/homo_sapiens"),
            ("tsv", "tsv/homo_sapiens"),
            ("mysql", f"mysql/{core}"),
        )
    )
    return tuple(files)


def parse_checksums(text: str) -> dict[str, tuple[int, int]]:
    entries: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            value = (int(fields[0]), int(fields[1]))
            if fields[2] in entries and entries[fields[2]] != value:
                raise SourceValidationError("Conflicting Ensembl CHECKSUMS entries")
            entries[fields[2]] = value
    if not entries:
        raise SourceValidationError("Empty or invalid Ensembl CHECKSUMS")
    return entries


def bsd_sum(path: Path) -> tuple[int, int]:
    """Ensembl publishes BSD sum of compressed bytes, with 1 KiB block counts."""
    checksum = size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            for value in chunk:
                checksum = (((checksum >> 1) | ((checksum & 1) << 15)) + value) & 0xFFFF
    return checksum, (size + 1023) // 1024


class EnsemblFtpVersionStrategy(SourceVersionStrategy):
    description = (
        "Reads the Ensembl release and verifies the human GRCh38 FTP file inventory "
        "against the release's three publisher CHECKSUMS manifests."
    )

    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return (ENSEMBL_RELEASE_URL,)

    def discover(self, http: HttpGateway, request: VersionProbeRequest) -> SourceVersion:
        response = http.get_json(
            ENSEMBL_RELEASE_URL,
            timeout=request.timeout.total_seconds(),
            headers={"Accept": "application/json"},
        )
        try:
            releases = response.payload["releases"]
            if not isinstance(releases, list) or not releases:
                raise ValueError("Missing releases")
            release = str(max(int(value) for value in releases))
            if int(release) <= 0:
                raise ValueError("Invalid release")
        except (KeyError, TypeError, ValueError) as error:
            raise SourceValidationError("Invalid Ensembl release response") from error
        specs = ensembl_ftp_files(release)
        hashes = {}
        for spec in specs[-3:]:
            text = http.get_text(spec.url, timeout=request.timeout.total_seconds()).text
            entries = parse_checksums(text)
            directory = spec.url.rsplit("/", 1)[0]
            for data in specs[:9]:
                if data.url.rsplit("/", 1)[0] == directory:
                    if data.url.rsplit("/", 1)[1] not in entries:
                        raise SourceValidationError(f"Missing checksum for {data.name}")
            hashes[spec.name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return SourceVersion(
            release,
            evidence={
                "method": "ensembl_ftp_release",
                "assembly": "GRCh38",
                "checksum_manifest_sha256": hashes,
            },
        )


class EnsemblHumanFtpSource(HttpSnapshotSource):
    @property
    def dataset(self) -> DatasetId:
        return DatasetId("ensembl", "human_ftp")

    @property
    def homepage(self) -> str:
        return "https://www.ensembl.org/"

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        # Templates describe the inventory without doing network I/O at startup.
        return ensembl_ftp_files("{release}")

    def file_specs_for(self, version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        if not version.value.isdigit():
            raise SourceValidationError("Ensembl FTP requires a numeric release")
        return ensembl_ftp_files(version.value)

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return EnsemblFtpVersionStrategy()

    def validate_confirmation(self, version: SourceVersion, confirmed: SourceVersion) -> None:
        super().validate_confirmation(version, confirmed)
        if version.evidence != confirmed.evidence:
            raise SourceValidationError("Ensembl CHECKSUMS changed during acquisition")

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        profiles: dict[str, Any] = {}
        expected_hashes = version.evidence["checksum_manifest_sha256"]
        try:
            for manifest_spec in self.file_specs_for(version)[-3:]:
                path = require_download(downloads, manifest_spec.name).resource.path
                text = path.read_text(encoding="utf-8")
                if (
                    hashlib.sha256(text.encode("utf-8")).hexdigest()
                    != expected_hashes[manifest_spec.name]
                ):
                    raise SourceValidationError("Ensembl CHECKSUMS changed before validation")
                entries = parse_checksums(text)
                directory = manifest_spec.url.rsplit("/", 1)[0]
                for item in downloads:
                    if item.spec.url.rsplit("/", 1)[0] != directory:
                        continue
                    filename = item.spec.url.rsplit("/", 1)[1]
                    if filename == "CHECKSUMS":
                        continue
                    if bsd_sum(item.resource.path) != entries.get(filename):
                        raise SourceValidationError(
                            f"Publisher checksum mismatch: {item.spec.name}"
                        )
            for item in downloads[:9]:
                profiles[item.spec.name] = profile_file(item.resource.path, item.spec.name)
        except (OSError, EOFError, UnicodeError) as error:
            raise SourceValidationError(f"Invalid Ensembl FTP file: {error}") from error
        return SourceValidationResult(
            version,
            {
                "validation": {"publisher_checksums_verified": True, "files": profiles},
                "assembly": "GRCh38",
                "species": "homo_sapiens",
            },
        )


def profile_file(path: Path, name: str) -> dict[str, Any]:
    if name == "ensembl_homo_sapiens.gff3.gz":
        genes = set()
        transcripts = cds = 0
        assembly = None
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("#!genome-version "):
                    assembly = line.strip().split(None, 1)[1]
                if line.startswith("#"):
                    continue
                fields = line.rstrip("\r\n").split("\t")
                if len(fields) != 9:
                    raise SourceValidationError("Malformed Ensembl GFF3 row")
                if fields[2] in {"gene", "ncRNA_gene", "pseudogene"}:
                    match = re.search(r"(?:^|;)(?:gene_id=|ID=gene:)(ENSG[^;]+)", fields[8])
                    if not match:
                        raise SourceValidationError("GFF3 gene lacks stable ENSG identifier")
                    genes.add(match.group(1))
                transcripts += "transcript_id=ENST" in fields[8]
                cds += fields[2] == "CDS" and "protein_id=ENSP" in fields[8]
        if assembly != "GRCh38" or len(genes) < MINIMUM_ROWS["gff3"] or not transcripts or not cds:
            raise SourceValidationError("Incomplete or wrong-assembly Ensembl GFF3")
        return {"genes": len(genes), "transcript_features": transcripts, "cds": cds}
    if name == "ensembl_mysql_schema.sql.gz":
        with gzip.open(path, "rt", encoding="latin-1") as handle:
            text = handle.read()
        for table in MYSQL_COLUMNS:
            if not re.search(rf"CREATE TABLE `{table}`\s*\(", text, re.IGNORECASE):
                raise SourceValidationError(f"Ensembl schema missing table {table}")
        return {"tables": list(MYSQL_COLUMNS)}
    mysql = name.startswith("ensembl_mysql_")
    kind = name.removeprefix("ensembl_mysql_" if mysql else "ensembl_").split(".")[0]
    expected = MYSQL_COLUMNS[kind] if mysql else len(XREF_HEADER)
    rows = 0
    with gzip.open(path, "rt", encoding="latin-1" if mysql else "utf-8") as handle:
        if not mysql and tuple(handle.readline().rstrip("\r\n").split("\t")) != XREF_HEADER:
            raise SourceValidationError(f"Unexpected Ensembl xref header: {name}")
        for line in handle:
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) != expected:
                raise SourceValidationError(f"Malformed Ensembl row in {name}")
            if not mysql and not fields[0].startswith("ENSG"):
                raise SourceValidationError(f"Missing human gene identifier in {name}")
            rows += 1
    if rows < MINIMUM_ROWS[kind]:
        raise SourceValidationError(f"Ensembl {name} has too few rows: {rows}")
    return {"rows": rows, "columns": expected, "gzip_valid": True}
