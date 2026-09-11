"""Sources whose version can only be established by inspecting downloaded bytes."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import tempfile
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
        payload = json.loads(resource.path.read_text(encoding="utf-8"))
        graph_meta = (payload.get("graphs") or [{}])[0].get("meta") or {}
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise SourceValidationError("Disease Ontology JSON metadata is invalid") from error
    for entry in graph_meta.get("basicPropertyValues") or []:
        if entry.get("pred") == "http://www.w3.org/2002/07/owl#versionInfo" and entry.get("val"):
            value = str(entry["val"])
            return SourceVersion(
                value,
                version_date=_iso_date_or_none(value),
                evidence={"type": "owl_version_info", "graph_version": value},
            )
    version_uri = graph_meta.get("version")
    if version_uri:
        value = str(version_uri).rstrip("/").rsplit("/", 2)[-2]
        return SourceVersion(
            value,
            version_date=_iso_date_or_none(value),
            evidence={"type": "owl_version_uri", "graph_version": str(version_uri)},
        )
    raise SourceValidationError("Disease Ontology JSON has no release version")


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
        HttpFileSpec("https://purl.obolibrary.org/obo/doid.json", "doid.json"),
        "https://disease-ontology.org/",
        "Downloads the ontology and reads its embedded OWL release version.",
        _disease_ontology_version,
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
