"""Release-coherent Ensembl human BioMart source export."""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.application.progress import ProgressUpdate
from ifx_registry.application.versioning import ensure_expected_version
from ifx_registry.domain.errors import SourceAcquisitionError, SourceValidationError
from ifx_registry.domain.models import DatasetId, SnapshotFile, SourceSnapshot, SourceVersion
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway
from ifx_registry.infrastructure.workspace import SnapshotWorkspace

ENSEMBL_DATASET = DatasetId("ensembl", "human_biomart")
ENSEMBL_RELEASE_URL = "https://rest.ensembl.org/info/data?"
ENSEMBL_ARCHIVE_URL = "https://www.ensembl.org/info/website/archives/index.html"
ENSEMBL_HOMEPAGE = "https://www.ensembl.org/"
ENSEMBL_BIOMART_ENDPOINTS = (
    "https://mart.ensembl.org/biomart/martservice",
    "https://www.ensembl.org/biomart/martservice",
)
ENSEMBL_EXPORT_REVISION = "export1"
ENSEMBL_SPECIES_DATASET = "hsapiens_gene_ensembl"


def archive_biomart_endpoint(release_date: date) -> str:
    archive_host = release_date.strftime("%b%Y").lower()
    return f"https://{archive_host}.archive.ensembl.org/biomart/martservice"


def _query(*attributes: str) -> str:
    rendered = "".join(f'<Attribute name="{attribute}" />' for attribute in attributes)
    return (
        '<!DOCTYPE Query><Query virtualSchemaName="default" '
        'formatter="TSV" header="1" uniqueRows="1" count="" '
        'datasetConfigVersion="0.6" completionStamp="1">'
        f'<Dataset name="{ENSEMBL_SPECIES_DATASET}" interface="default">'
        f"{rendered}</Dataset></Query>"
    )


@dataclass(frozen=True, slots=True)
class BioMartExport:
    name: str
    query: str
    expected_header: tuple[str, ...]
    minimum_rows: int
    delimiter: str = ","

    @property
    def query_sha256(self) -> str:
        return hashlib.sha256(self.query.encode("utf-8")).hexdigest()


ENSEMBL_EXPORTS = (
    BioMartExport(
        "gene_transcript_identifiers.csv",
        _query(
            "ensembl_gene_id",
            "ensembl_gene_id_version",
            "ensembl_transcript_id",
            "ensembl_transcript_id_version",
            "ensembl_peptide_id",
            "ensembl_peptide_id_version",
            "external_gene_name",
            "gene_biotype",
            "transcript_is_canonical",
            "external_synonym",
            "transcript_tsl",
            "entrezgene_id",
            "hgnc_id",
        ),
        (
            "Gene stable ID",
            "Gene stable ID version",
            "Transcript stable ID",
            "Transcript stable ID version",
            "Protein stable ID",
            "Protein stable ID version",
            "Gene name",
            "Gene type",
            "Ensembl Canonical",
            "Gene Synonym",
            "Transcript support level (TSL)",
            "NCBI gene (formerly Entrezgene) ID",
            "HGNC ID",
        ),
        1_000_000,
    ),
    BioMartExport(
        "uniprot_mappings.csv",
        _query(
            "ensembl_gene_id",
            "ensembl_gene_id_version",
            "ensembl_transcript_id",
            "ensembl_transcript_id_version",
            "ensembl_peptide_id",
            "ensembl_peptide_id_version",
            "uniprotswissprot",
            "uniprotsptrembl",
            "uniprot_isoform",
        ),
        (
            "Gene stable ID",
            "Gene stable ID version",
            "Transcript stable ID",
            "Transcript stable ID version",
            "Protein stable ID",
            "Protein stable ID version",
            "UniProtKB/Swiss-Prot ID",
            "UniProtKB/TrEMBL ID",
            "UniProtKB isoform ID",
        ),
        300_000,
    ),
    BioMartExport(
        "refseq_mappings.csv",
        _query(
            "ensembl_gene_id",
            "ensembl_gene_id_version",
            "ensembl_transcript_id",
            "ensembl_transcript_id_version",
            "transcript_mane_select",
            "refseq_mrna",
            "refseq_ncrna",
            "refseq_peptide",
        ),
        (
            "Gene stable ID",
            "Gene stable ID version",
            "Transcript stable ID",
            "Transcript stable ID version",
            "RefSeq match transcript (MANE Select)",
            "RefSeq mRNA ID",
            "RefSeq ncRNA ID",
            "RefSeq peptide ID",
        ),
        500_000,
    ),
    BioMartExport(
        "gene_coordinates.csv",
        _query(
            "ensembl_gene_id",
            "ensembl_gene_id_version",
            "description",
            "chromosome_name",
            "strand",
            "start_position",
            "end_position",
        ),
        (
            "Gene stable ID",
            "Gene stable ID version",
            "Gene description",
            "Chromosome/scaffold name",
            "Strand",
            "Gene start (bp)",
            "Gene end (bp)",
        ),
        80_000,
    ),
    BioMartExport(
        "transcript_attributes.tsv",
        _query(
            "ensembl_gene_id",
            "external_transcript_name",
            "external_gene_name",
            "ensembl_transcript_id",
            "ensembl_transcript_id_version",
            "transcript_biotype",
            "transcript_start",
            "transcript_end",
            "transcription_start_site",
            "transcript_length",
        ),
        (
            "Gene stable ID",
            "Transcript name",
            "Gene name",
            "Transcript stable ID",
            "Transcript stable ID version",
            "Transcript type",
            "Transcript start (bp)",
            "Transcript end (bp)",
            "Transcription start site (TSS)",
            "Transcript length (including UTRs and CDS)",
        ),
        200_000,
        delimiter="\t",
    ),
)


def biomart_url(endpoint: str, query: str) -> str:
    return f"{endpoint}?{urlencode({'query': query})}"


class EnsemblHumanBioMartSource(SourceAdapter):
    """Five reviewed BioMart exports captured under one Ensembl release."""

    def __init__(
        self,
        http: HttpGateway,
        *,
        exports: tuple[BioMartExport, ...] = ENSEMBL_EXPORTS,
        endpoints: tuple[str, ...] = ENSEMBL_BIOMART_ENDPOINTS,
    ):
        if not exports:
            raise ValueError("Ensembl exports must not be empty")
        if not endpoints:
            raise ValueError("Ensembl BioMart endpoints must not be empty")
        self._http = http
        self._exports = exports
        self._endpoints = endpoints

    @property
    def dataset(self) -> DatasetId:
        return ENSEMBL_DATASET

    @property
    def homepage(self) -> str:
        return ENSEMBL_HOMEPAGE

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (ENSEMBL_RELEASE_URL, ENSEMBL_ARCHIVE_URL, *self._endpoints)

    @property
    def version_check_description(self) -> str:
        return (
            "Reads the current Ensembl release from the official REST API before and "
            "after running the five human BioMart exports."
        )

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return (ENSEMBL_RELEASE_URL, ENSEMBL_ARCHIVE_URL)

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        response = self._http.get_json(
            ENSEMBL_RELEASE_URL,
            timeout=request.timeout.total_seconds(),
            headers={"Content-Type": "application/json"},
        )
        payload = response.payload
        releases = payload.get("releases") if isinstance(payload, dict) else None
        if not isinstance(releases, list) or not releases:
            raise SourceValidationError(
                "Ensembl release response did not contain numeric releases"
            )
        try:
            release = max(int(value) for value in releases)
        except (TypeError, ValueError) as error:
            raise SourceValidationError(
                "Ensembl release response did not contain numeric releases"
            ) from error

        evidence: dict[str, object] = {
            "method": "ensembl_info_data",
            "upstream_release": str(release),
            "export_revision": ENSEMBL_EXPORT_REVISION,
            "url": response.metadata.final_url,
        }
        archive = self._http.get_text(
            ENSEMBL_ARCHIVE_URL,
            timeout=request.timeout.total_seconds(),
        )
        match = re.search(
            rf"(?:Ensembl\s+|release\s+){release}\b.*?([A-Z][a-z]{{2}}\s+\d{{4}})",
            archive.text,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            raise SourceValidationError(
                f"Ensembl archive page did not identify the release date for {release}"
            )
        release_date = datetime.strptime(match.group(1).title(), "%b %Y").date()
        evidence["release_date_label"] = match.group(1)
        evidence["biomart_endpoint"] = archive_biomart_endpoint(release_date)

        return SourceVersion(
            f"{release}-{ENSEMBL_EXPORT_REVISION}",
            version_date=release_date,
            discovered_at=datetime.now(UTC),
            evidence=evidence,
        )

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        request.progress.report(ProgressUpdate("checking", "Checking the Ensembl release"))
        version = self.discover_latest(VersionProbeRequest(timeout=request.timeout))
        ensure_expected_version(self.dataset, version, request.expected_version)
        snapshot_dir = request.destination / "ensembl" / "human_biomart" / version.value
        validations: dict[str, object] = {}
        snapshot_files: list[SnapshotFile] = []

        with SnapshotWorkspace(snapshot_dir) as workspace:
            for index, export in enumerate(self._exports, start=1):
                request.progress.report(
                    ProgressUpdate(
                        "downloading",
                        f"Exporting {export.name}",
                        index,
                        len(self._exports),
                    )
                )
                resource, metadata = self._fetch_export(
                    export,
                    workspace.staging_path,
                    request.timeout.total_seconds(),
                    endpoints=self._fetch_endpoints(version),
                )
                validations[export.name] = metadata
                snapshot_files.append(
                    SnapshotFile(
                        local_path=snapshot_dir / export.name,
                        relative_path=PurePosixPath(export.name),
                        source_url=resource.metadata.final_url,
                        content_type=(
                            "text/tab-separated-values"
                            if export.delimiter == "\t"
                            else "text/csv"
                        ),
                    )
                )

            request.progress.report(
                ProgressUpdate("confirming", "Confirming the Ensembl release did not change")
            )
            confirmed = self.discover_latest(VersionProbeRequest(timeout=request.timeout))
            ensure_expected_version(self.dataset, confirmed, version)
            workspace.commit()

        return SourceSnapshot(
            dataset=self.dataset,
            version=version,
            files=tuple(snapshot_files),
            downloaded_at=datetime.now(UTC),
            homepage=self.homepage,
            upstream_urls=self.upstream_urls,
            metadata={
                "upstream_release": version.evidence["upstream_release"],
                "export_revision": ENSEMBL_EXPORT_REVISION,
                "species_dataset": ENSEMBL_SPECIES_DATASET,
                "taxon_id": 9606,
                "exports": validations,
            },
        )

    def _fetch_endpoints(self, version: SourceVersion) -> tuple[str, ...]:
        if self._endpoints != ENSEMBL_BIOMART_ENDPOINTS:
            return self._endpoints
        if version.version_date is None:
            raise SourceValidationError("Ensembl release date is required for acquisition")
        return (archive_biomart_endpoint(version.version_date),)

    def _fetch_export(
        self,
        export: BioMartExport,
        staging_dir: Path,
        timeout: float,
        *,
        endpoints: tuple[str, ...],
    ) -> tuple[DownloadedResource, dict[str, object]]:
        errors: list[str] = []
        raw_path = staging_dir / f".{export.name}.download"
        output_path = staging_dir / export.name
        for endpoint in endpoints:
            url = biomart_url(endpoint, export.query)
            raw_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
            try:
                resource = self._http.download(url, raw_path, timeout=timeout)
                row_count = _validate_and_convert(export, raw_path, output_path)
                raw_path.unlink(missing_ok=True)
                return resource, {
                    "query_sha256": export.query_sha256,
                    "header": list(export.expected_header),
                    "rows": row_count,
                    "minimum_rows": export.minimum_rows,
                    "completion_stamp": True,
                    "endpoint": endpoint,
                }
            except (OSError, SourceAcquisitionError, SourceValidationError) as error:
                errors.append(f"{endpoint}: {error}")
        raw_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        raise SourceValidationError(
            f"Could not produce Ensembl BioMart export {export.name}: " + " | ".join(errors)
        )


def _validate_and_convert(export: BioMartExport, source: Path, destination: Path) -> int:
    row_count = 0
    with source.open("r", encoding="utf-8", errors="replace", newline="") as input_handle:
        reader = csv.reader(input_handle, delimiter="\t")
        header = next((row for row in reader if row and any(value.strip() for value in row)), None)
        if header is None or _looks_like_error(header):
            raise SourceValidationError(f"{export.name} contained an empty or error response")
        if tuple(header) != export.expected_header:
            raise SourceValidationError(
                f"{export.name} returned unexpected header {header!r}; "
                f"expected {list(export.expected_header)!r}"
            )

        with destination.open("w", encoding="utf-8", newline="") as output_handle:
            writer = csv.writer(output_handle, delimiter=export.delimiter)
            writer.writerow(header)
            pending: list[str] | None = None
            for row in reader:
                if not row or not any(value.strip() for value in row):
                    continue
                if pending is not None:
                    _write_data_row(export, writer, pending)
                    row_count += 1
                pending = row

            if pending != ["[success]"]:
                raise SourceValidationError(
                    f"{export.name} did not end with the BioMart completion stamp"
                )

    if row_count < export.minimum_rows:
        destination.unlink(missing_ok=True)
        raise SourceValidationError(
            f"{export.name} row count {row_count} is below minimum {export.minimum_rows}"
        )
    return row_count


def _write_data_row(export: BioMartExport, writer: object, row: list[str]) -> None:
    if len(row) != len(export.expected_header):
        raise SourceValidationError(
            f"{export.name} contained a row with {len(row)} fields; "
            f"expected {len(export.expected_header)}"
        )
    writer.writerow(row)  # type: ignore[attr-defined]


def _looks_like_error(row: list[str]) -> bool:
    first = (row[0] if row else "").strip().casefold()
    return (
        not first
        or first.startswith("<")
        or first.startswith("query error")
        or "biomart::exception" in first
        or "could not connect to mysql database" in first
        or "can't connect to mysql server" in first
    )
