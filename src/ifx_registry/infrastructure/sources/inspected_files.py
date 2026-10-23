"""Sources whose version can only be established by inspecting downloaded bytes."""

from __future__ import annotations

import csv
import gzip
import hashlib
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.application.progress import ProgressUpdate
from ifx_registry.application.versioning import ensure_expected_version
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SnapshotFile, SourceSnapshot, SourceVersion
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway
from ifx_registry.infrastructure.sources.http_snapshot import HttpFileSpec
from ifx_registry.infrastructure.workspace import SnapshotWorkspace

VersionInspector = Callable[[DownloadedResource], SourceVersion]


@dataclass(frozen=True, slots=True)
class InspectedFileSourceDefinition:
    dataset: DatasetId
    file: HttpFileSpec
    homepage: str
    version_description: str
    inspector: VersionInspector


class InspectedFileSource(SourceAdapter):
    """Download one file and identify its version from the downloaded content."""

    def __init__(self, http: HttpGateway, definition: InspectedFileSourceDefinition):
        self._http = http
        self._definition = definition

    @property
    def dataset(self) -> DatasetId:
        return self._definition.dataset

    @property
    def homepage(self) -> str:
        return self._definition.homepage

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (self._definition.file.url,)

    @property
    def version_check_description(self) -> str:
        return self._definition.version_description

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return (self._definition.file.url,)

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        with tempfile.TemporaryDirectory(prefix="ifx-registry-version-") as temporary:
            resource = self._http.download(
                self._definition.file.url,
                Path(temporary) / self._definition.file.name,
                timeout=request.timeout.total_seconds(),
            )
            return self._definition.inspector(resource)

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        request.progress.report(
            ProgressUpdate(
                stage="downloading",
                message=f"Downloading {self._definition.file.name}",
                completed=1,
                total=1,
            )
        )
        with tempfile.TemporaryDirectory(prefix="ifx-registry-acquisition-") as temporary:
            resource = self._http.download(
                self._definition.file.url,
                Path(temporary) / self._definition.file.name,
                timeout=request.timeout.total_seconds(),
            )
            request.progress.report(
                ProgressUpdate(
                    stage="validating",
                    message=f"Reading version metadata from {self._definition.file.name}",
                )
            )
            version = self._definition.inspector(resource)
            ensure_expected_version(self.dataset, version, request.expected_version)
            snapshot_dir = (
                request.destination / self.dataset.source / self.dataset.dataset / version.value
            )
            with SnapshotWorkspace(snapshot_dir) as workspace:
                staged = workspace.staging_path / self._definition.file.name
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(resource.path, staged)
                workspace.commit()

        return SourceSnapshot(
            dataset=self.dataset,
            version=version,
            files=(
                SnapshotFile(
                    snapshot_dir / self._definition.file.name,
                    PurePosixPath(self._definition.file.name),
                    resource.metadata.final_url,
                    resource.metadata.header("content-type"),
                ),
            ),
            downloaded_at=datetime.now(UTC),
            homepage=self.homepage,
            upstream_urls=self.upstream_urls,
            metadata={"version_method": dict(version.evidence)},
        )


def _ctd_version(resource: DownloadedResource) -> SourceVersion:
    report_created = None
    with gzip.open(resource.path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("# Report created:"):
                report_created = line.split(":", 1)[1].strip()
                break
            if not line.startswith("#"):
                break
    if not report_created:
        raise SourceValidationError("CTD file does not contain a Report created header")
    match = re.search(
        r"([A-Z][a-z]{2} [A-Z][a-z]{2} \d{2} \d{2}:\d{2}:\d{2} [A-Z]{3,4} \d{4})$",
        report_created,
    )
    if match is None:
        raise SourceValidationError(f"Could not parse CTD report date {report_created!r}")
    parts = match.group(1).split()
    parsed = datetime.strptime(" ".join([*parts[:4], parts[-1]]), "%a %b %d %H:%M:%S %Y")
    version_date = parsed.date()
    return SourceVersion(
        version_date.isoformat(),
        version_date=version_date,
        evidence={"type": "downloaded_file_header", "report_created": report_created},
    )


def _mp_version(resource: DownloadedResource) -> SourceVersion:
    with resource.path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index > 200:
                break
            if line.startswith("data-version:"):
                raw = line.split(":", 1)[1].strip()
                parts = [part for part in raw.rstrip("/").split("/") if part]
                value = parts[-2] if len(parts) >= 2 and parts[-1] == "mp.obo" else parts[-1]
                parsed_date = _iso_date_or_none(value)
                return SourceVersion(
                    value,
                    version_date=parsed_date,
                    evidence={"type": "obo_data_version", "data_version": raw},
                )
    raw_modified = resource.metadata.header("Last-Modified")
    if raw_modified:
        version_date = _http_date(raw_modified)
        return SourceVersion(
            version_date.isoformat(),
            version_date=version_date,
            evidence={"type": "last_modified", "last_modified": raw_modified},
        )
    raise SourceValidationError("MP ontology has no data-version or Last-Modified date")


def _disease_ontology_version(resource: DownloadedResource) -> SourceVersion:
    try:
        with resource.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("[Term]"):
                    break
                if line.startswith("data-version: "):
                    raw = line.removeprefix("data-version: ").strip()
                    match = re.fullmatch(r"releases/(\d{4}-\d{2}-\d{2})/doid\.obo", raw)
                    if match is None:
                        raise SourceValidationError(
                            f"Disease Ontology OBO has invalid data-version: {raw}"
                        )
                    value = match.group(1)
                    version_date = _iso_date_or_none(value)
                    if version_date is None:
                        raise SourceValidationError(
                            f"Disease Ontology OBO has invalid release date: {value}"
                        )
                    return SourceVersion(
                        value,
                        version_date=version_date,
                        evidence={"type": "obo_data_version", "data_version": raw},
                    )
    except OSError as error:
        raise SourceValidationError("Could not read Disease Ontology OBO header") from error
    raise SourceValidationError("Disease Ontology OBO has no data-version header")


def _efo_version(resource: DownloadedResource) -> SourceVersion:
    try:
        with resource.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("[Term]"):
                    break
                if line.startswith("data-version: "):
                    raw = line.removeprefix("data-version: ").strip()
                    match = re.fullmatch(
                        r"https?://www\.ebi\.ac\.uk/efo/releases/(v\d+\.\d+\.\d+)/efo\.owl",
                        raw,
                    )
                    if match is None:
                        raise SourceValidationError(
                            f"EFO OBO has invalid data-version: {raw}"
                        )
                    return SourceVersion(
                        match.group(1),
                        evidence={"type": "obo_data_version", "data_version": raw},
                    )
    except OSError as error:
        raise SourceValidationError("Could not read EFO OBO header") from error
    raise SourceValidationError("EFO OBO has no data-version header")


def _hpo_version(resource: DownloadedResource) -> SourceVersion:
    data_version = None
    ontology = None
    has_term = False
    try:
        with resource.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("[Term]"):
                    has_term = True
                    break
                if line.startswith("data-version: "):
                    data_version = line.removeprefix("data-version: ").strip()
                elif line.startswith("ontology: "):
                    ontology = line.removeprefix("ontology: ").strip()
    except (OSError, UnicodeError) as error:
        raise SourceValidationError("Could not read HPO OBO header") from error
    if ontology != "hp.obo":
        raise SourceValidationError(f"HPO OBO has unexpected ontology: {ontology!r}")
    if not has_term:
        raise SourceValidationError("HPO OBO has no terms")
    match = re.fullmatch(r"hp/releases/(\d{4}-\d{2}-\d{2})", data_version or "")
    if match is None:
        raise SourceValidationError(f"HPO OBO has invalid data-version: {data_version!r}")
    version_date = _iso_date_or_none(match.group(1))
    if version_date is None:
        raise SourceValidationError(f"HPO OBO has invalid release date: {match.group(1)}")
    return SourceVersion(
        version_date.isoformat(),
        version_date=version_date,
        evidence={"type": "obo_data_version", "data_version": data_version},
    )


def _ncit_version(resource: DownloadedResource) -> SourceVersion:
    data_version = None
    ontology = None
    has_term = False
    try:
        with resource.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("[Term]"):
                    has_term = True
                    break
                if line.startswith("data-version: "):
                    data_version = line.removeprefix("data-version: ").strip()
                elif line.startswith("ontology: "):
                    ontology = line.removeprefix("ontology: ").strip()
    except (OSError, UnicodeError) as error:
        raise SourceValidationError("Could not read NCIt OBO header") from error
    if ontology != "ncit":
        raise SourceValidationError(f"NCIt OBO has unexpected ontology: {ontology!r}")
    if not has_term:
        raise SourceValidationError("NCIt OBO has no terms")
    match = re.fullmatch(r"releases/(\d{4}-\d{2}-\d{2})", data_version or "")
    if match is None:
        raise SourceValidationError(f"NCIt OBO has invalid data-version: {data_version!r}")
    version_date = _iso_date_or_none(match.group(1))
    if version_date is None:
        raise SourceValidationError(f"NCIt OBO has invalid release date: {match.group(1)}")
    return SourceVersion(
        version_date.isoformat(),
        version_date=version_date,
        evidence={"type": "obo_data_version", "data_version": data_version},
    )


def _orphanet_gene_associations_version(resource: DownloadedResource) -> SourceVersion:
    exported_at = None
    declared_disorders = None
    disorders = 0
    try:
        for event, element in ET.iterparse(resource.path, events=("start", "end")):
            tag = element.tag.rsplit("}", 1)[-1]
            if event == "start" and tag == "JDBOR":
                exported_at = element.get("date")
            elif event == "start" and tag == "DisorderList":
                declared_disorders = element.get("count")
            elif event == "end":
                if tag == "Disorder":
                    disorders += 1
                element.clear()
    except (OSError, ET.ParseError) as error:
        raise SourceValidationError(
            f"Could not parse Orphadata gene-association XML: {error}"
        ) from error
    try:
        exported = datetime.strptime(exported_at or "", "%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        raise SourceValidationError(
            f"Orphadata gene-association XML has invalid export date {exported_at!r}"
        ) from error
    if not declared_disorders or not declared_disorders.isdigit():
        raise SourceValidationError("Orphadata gene-association XML lacks DisorderList count")
    if disorders == 0 or disorders != int(declared_disorders):
        raise SourceValidationError(
            f"Orphadata gene-association XML has {disorders} disorders; "
            f"declared {declared_disorders}"
        )
    return SourceVersion(
        exported.date().isoformat(),
        version_date=exported.date(),
        evidence={
            "type": "xml_export_date",
            "exported_at": exported_at,
            "disorders": disorders,
        },
    )


def _clingen_version(resource: DownloadedResource) -> SourceVersion:
    required_columns = {
        "GENE SYMBOL", "GENE ID (HGNC)", "DISEASE LABEL", "DISEASE ID (MONDO)",
        "CLASSIFICATION", "CLASSIFICATION DATE",
    }
    try:
        with resource.path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            title = next(reader)
            created = next(reader)
            next(reader)
            next(reader)
            columns = next(reader)
            next(reader)
            first_data = next(reader)
    except (OSError, UnicodeError, csv.Error, StopIteration) as error:
        raise SourceValidationError(
            "ClinGen gene-validity CSV is unreadable or incomplete"
        ) from error
    if not title or title[0] != "CLINGEN GENE DISEASE VALIDITY CURATIONS":
        raise SourceValidationError("ClinGen gene-validity CSV has an unexpected title")
    if not created:
        raise SourceValidationError("ClinGen gene-validity CSV has no creation date")
    match = re.fullmatch(r"FILE CREATED: (\d{4}-\d{2}-\d{2})", created[0])
    if match is None:
        raise SourceValidationError(
            f"ClinGen gene-validity CSV has an invalid creation date: {created[0]!r}"
        )
    version_date = _iso_date_or_none(match.group(1))
    if version_date is None:
        raise SourceValidationError("ClinGen gene-validity CSV has an invalid calendar date")
    if not required_columns.issubset(columns):
        raise SourceValidationError("ClinGen gene-validity CSV is missing required columns")
    if len(first_data) != len(columns) or not all(first_data[index] for index in (0, 1, 2)):
        raise SourceValidationError("ClinGen gene-validity CSV has no valid data row")
    digest = hashlib.sha256(resource.path.read_bytes()).hexdigest()
    return SourceVersion(
        f"{version_date.isoformat()}-sha256-{digest[:12]}",
        version_date=version_date,
        evidence={
            "type": "csv_file_created_and_content_hash",
            "file_created": match.group(1),
            "sha256": digest,
        },
    )


def _refmet_version(resource: DownloadedResource) -> SourceVersion:
    digest = hashlib.sha256(resource.path.read_bytes()).hexdigest()
    today = datetime.now(UTC).date()
    return SourceVersion(
        f"sha256-{digest[:12]}",
        version_date=today,
        evidence={"type": "content_hash", "sha256": digest},
    )


def _lipidmaps_version(resource: DownloadedResource) -> SourceVersion:
    try:
        with zipfile.ZipFile(resource.path) as archive:
            year, month, day, *_ = archive.getinfo("structures.sdf").date_time
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise SourceValidationError(
            "LIPID MAPS archive does not contain a valid structures.sdf"
        ) from error
    version_date = date(year, month, day)
    return SourceVersion(
        version_date.isoformat(),
        version_date=version_date,
        evidence={"type": "zip_inner_file_timestamp", "inner_file": "structures.sdf"},
    )


def _http_date(value: str) -> date:
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError) as error:
        raise SourceValidationError(f"Could not parse Last-Modified {value!r}") from error


def _iso_date_or_none(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


INSPECTED_FILE_SOURCES: dict[str, InspectedFileSourceDefinition] = {
    "ctd_curated_genes_diseases": InspectedFileSourceDefinition(
        DatasetId("ctd", "curated_genes_diseases"),
        HttpFileSpec(
            "https://ctdbase.org/reports/CTD_curated_genes_diseases.tsv.gz",
            "CTD_curated_genes_diseases.tsv.gz",
        ),
        "https://ctdbase.org/",
        "Downloads the report and reads its embedded creation date.",
        _ctd_version,
    ),
    "mp_ontology": InspectedFileSourceDefinition(
        DatasetId("mp", "ontology"),
        HttpFileSpec("https://purl.obolibrary.org/obo/mp.obo", "mp.obo"),
        "https://obofoundry.org/ontology/mp.html",
        "Downloads the ontology and reads its data-version header.",
        _mp_version,
    ),
    "disease_ontology": InspectedFileSourceDefinition(
        DatasetId("disease_ontology", "ontology"),
        HttpFileSpec("https://purl.obolibrary.org/obo/doid.obo", "doid.obo"),
        "https://disease-ontology.org/",
        "Downloads the ontology and reads its OBO data-version header.",
        _disease_ontology_version,
    ),
    "efo_ontology": InspectedFileSourceDefinition(
        DatasetId("efo", "ontology"),
        HttpFileSpec("https://www.ebi.ac.uk/efo/efo.obo", "efo.obo"),
        "https://www.ebi.ac.uk/efo/",
        "Downloads the ontology and reads its OBO data-version header.",
        _efo_version,
    ),
    "hpo_ontology": InspectedFileSourceDefinition(
        DatasetId("hpo", "ontology"),
        HttpFileSpec("https://purl.obolibrary.org/obo/hp.obo", "hp.obo"),
        "https://obofoundry.org/ontology/hp.html",
        "Downloads the ontology and reads its OBO data-version header.",
        _hpo_version,
    ),
    "ncit_ontology": InspectedFileSourceDefinition(
        DatasetId("ncit", "ontology"),
        HttpFileSpec("https://purl.obolibrary.org/obo/ncit.obo", "ncit.obo"),
        "https://github.com/ncit-obo-org/ncit-obo-edition",
        "Downloads the ontology and reads its OBO data-version header.",
        _ncit_version,
    ),
    "orphanet_gene_associations": InspectedFileSourceDefinition(
        DatasetId("orphanet", "gene_associations"),
        HttpFileSpec(
            "https://www.orphadata.com/data/xml/en_product6.xml",
            "orphanet_gene_associations.xml",
        ),
        "https://sciences.orphadata.com/genes/",
        "Downloads the XML and reads its embedded JDBOR export date.",
        _orphanet_gene_associations_version,
    ),
    "clingen_gene_validity": InspectedFileSourceDefinition(
        DatasetId("clingen", "gene_validity"),
        HttpFileSpec(
            "https://search.clinicalgenome.org/kb/gene-validity/download",
            "clingen_gene_validity.csv",
        ),
        "https://search.clinicalgenome.org/kb/gene-validity",
        "Downloads the CSV and versions it by FILE CREATED date and content hash.",
        _clingen_version,
    ),
    "refmet_metabolites_csv": InspectedFileSourceDefinition(
        DatasetId("refmet", "metabolites_csv"),
        HttpFileSpec(
            "https://www.metabolomicsworkbench.org/databases/refmet/refmet_download.php",
            "refmet.csv",
        ),
        "https://www.metabolomicsworkbench.org/databases/refmet/browse.php",
        "Downloads the CSV and uses its content hash as the immutable version.",
        _refmet_version,
    ),
    "lipidmaps_lmsd_sdf": InspectedFileSourceDefinition(
        DatasetId("lipidmaps", "lmsd_sdf"),
        HttpFileSpec("https://www.lipidmaps.org/files/?file=LMSD&ext=sdf.zip", "LMSD.sdf.zip"),
        "https://www.lipidmaps.org/",
        "Downloads the archive and reads the structures.sdf timestamp inside it.",
        _lipidmaps_version,
    ),
}
