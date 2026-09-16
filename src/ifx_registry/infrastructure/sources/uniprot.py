"""UniProt human proteome source declaration and validation."""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from io import TextIOWrapper
from pathlib import Path
from time import sleep
from urllib.parse import urlencode, urlsplit

import ijson

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.application.progress import ProgressUpdate
from ifx_registry.application.versioning import ensure_expected_version
from ifx_registry.domain.errors import SourceAcquisitionError, SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway, HttpJson
from ifx_registry.infrastructure.sources._dates import parse_flexible_date
from ifx_registry.infrastructure.sources.api_exports import (
    GeneratedArtifact,
    GeneratedSnapshotSource,
)
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
    require_download,
)
from ifx_registry.infrastructure.sources.version_strategies import (
    HeaderVersionStrategy,
    SourceVersionStrategy,
)

UNIPROT_RELEASE_PROBE_URL = (
    "https://rest.uniprot.org/uniprotkb/stream?"
    "compressed=false&format=json&size=1&query=accession:P04637"
)
UNIPROT_HUMAN_URL = (
    "https://rest.uniprot.org/uniprotkb/stream?"
    "compressed=true&format=json&query=(*)+AND+(model_organism:9606)"
)
UNIPROT_REVIEWED_HUMAN_URL = (
    "https://rest.uniprot.org/uniprotkb/stream?"
    "compressed=true&format=json&query=(reviewed:true)+AND+(model_organism:9606)"
)
UNIPROT_HUMAN_REFERENCE_PROTEOME_QUERY = "(proteome:UP000005640)"
UNIPROT_HUMAN_REFERENCE_PROTEOME_PAGE_SIZE = 500
UNIPROT_HUMAN_REFERENCE_PROTEOME_URL = (
    "https://rest.uniprot.org/uniprotkb/search?"
    + urlencode(
        {
            "format": "json",
            "query": UNIPROT_HUMAN_REFERENCE_PROTEOME_QUERY,
            "size": UNIPROT_HUMAN_REFERENCE_PROTEOME_PAGE_SIZE,
            "sort": "accession asc",
        }
    )
)
UNIPROT_HUMAN_IDMAPPING_URL = (
    "https://ftp.ebi.ac.uk/pub/databases/uniprot/current_release/knowledgebase/"
    "idmapping/by_organism/HUMAN_9606_idmapping.dat.gz"
)
UNIPROT_HOMEPAGE = "https://www.uniprot.org/"
UNIPROT_HUMAN_NAME = "uniprot-human.json.gz"
UNIPROT_REVIEWED_HUMAN_NAME = "uniprot-human-reviewed.json.gz"
UNIPROT_HUMAN_REFERENCE_PROTEOME_NAME = "uniprotkb_taxonomy_id_9606.json.gz"
UNIPROT_HUMAN_IDMAPPING_NAME = "HUMAN_9606_idmapping.dat.gz"
UNIPROT_REFERENCE_PROTEOME_MINIMUM_RECORDS = 100_000
UNIPROT_HUMAN_IDMAPPING_MINIMUM_ROWS = 1_000_000

UNIPROT_FILES = (
    HttpFileSpec(UNIPROT_HUMAN_URL, UNIPROT_HUMAN_NAME),
    HttpFileSpec(UNIPROT_REVIEWED_HUMAN_URL, UNIPROT_REVIEWED_HUMAN_NAME),
)


def normalize_uniprot_release_date(value: str | None) -> date | None:
    """Normalize the optional UniProt release-date header."""
    if not value:
        return None
    return parse_flexible_date(value, field_name="UniProt release")


def _parse_required_uniprot_release_date(value: str) -> date:
    parsed = normalize_uniprot_release_date(value)
    if parsed is None:
        raise SourceValidationError("UniProt release date was blank")
    return parsed


UNIPROT_VERSION_STRATEGY = HeaderVersionStrategy(
    url=UNIPROT_RELEASE_PROBE_URL,
    version_header="X-UniProt-Release",
    evidence_method="uniprot_release_headers",
    missing_version_message="UniProt response did not include X-UniProt-Release",
    description=(
        "Reads the X-UniProt-Release and X-UniProt-Release-Date response headers "
        "before and after downloading the files."
    ),
    date_header="X-UniProt-Release-Date",
    date_parser=_parse_required_uniprot_release_date,
)


@dataclass(frozen=True, slots=True)
class AccessionScan:
    records: int
    primary: frozenset[str]
    secondary: frozenset[str]
    reviewed_primary: frozenset[str]


def _scan_accessions(path: Path) -> AccessionScan:
    primary: set[str] = set()
    secondary: set[str] = set()
    reviewed_primary: set[str] = set()
    records = 0

    try:
        with gzip.open(path, "rb") as handle:
            for item in ijson.items(handle, "results.item"):
                if not isinstance(item, Mapping):
                    raise SourceValidationError(f"UniProt record in {path.name} was not an object")
                records += 1
                primary_accession = item.get("primaryAccession")
                if isinstance(primary_accession, str) and primary_accession:
                    primary.add(primary_accession)
                secondary_accessions = item.get("secondaryAccessions") or []
                if isinstance(secondary_accessions, list):
                    secondary.update(
                        accession
                        for accession in secondary_accessions
                        if isinstance(accession, str)
                    )
                entry_type = str(item.get("entryType") or "").casefold()
                if (
                    isinstance(primary_accession, str)
                    and primary_accession
                    and "reviewed" in entry_type
                    and "unreviewed" not in entry_type
                ):
                    reviewed_primary.add(primary_accession)
    except (OSError, ValueError, ijson.JSONError) as error:
        raise SourceValidationError(f"Could not parse UniProt download {path}: {error}") from error

    if records == 0:
        raise SourceValidationError(f"UniProt download {path} contained no result records")
    return AccessionScan(
        records=records,
        primary=frozenset(primary),
        secondary=frozenset(secondary),
        reviewed_primary=frozenset(reviewed_primary),
    )


def validate_reviewed_in_full(full_path: Path, reviewed_path: Path) -> dict[str, int]:
    """Ensure the reviewed human subset is contained in the complete human release."""
    full = _scan_accessions(full_path)
    reviewed = _scan_accessions(reviewed_path)
    full_primary = full.primary
    full_secondary = full.secondary
    reviewed_primary = reviewed.primary
    reviewed_secondary = reviewed.secondary
    missing_primary = sorted(reviewed_primary - full_primary)
    missing_secondary = sorted(reviewed_secondary - (full_primary | full_secondary))

    stats = {
        "full_records": full.records,
        "full_primary_accessions": len(full_primary),
        "full_secondary_accessions": len(full_secondary),
        "full_reviewed_primary_accessions": len(full.reviewed_primary),
        "reviewed_records": reviewed.records,
        "reviewed_primary_accessions": len(reviewed_primary),
        "reviewed_secondary_accessions": len(reviewed_secondary),
        "missing_reviewed_primary_accessions": len(missing_primary),
        "missing_reviewed_secondary_accessions": len(missing_secondary),
    }
    if missing_primary or missing_secondary:
        raise SourceValidationError(
            "UniProt full human download does not contain every reviewed accession: "
            f"{len(missing_primary)} primary and {len(missing_secondary)} secondary missing. "
            f"Primary sample: {', '.join(missing_primary[:20]) or 'none'}. "
            f"Secondary sample: {', '.join(missing_secondary[:20]) or 'none'}."
        )
    return stats


class UniProtHumanSource(HttpSnapshotSource):
    """UniProt's complete and reviewed human JSON streams."""

    _dataset = DatasetId(source="uniprot", dataset="human")

    def __init__(self, http: HttpGateway):
        super().__init__(http)

    @property
    def dataset(self) -> DatasetId:
        return self._dataset

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return UNIPROT_FILES

    @property
    def homepage(self) -> str:
        return UNIPROT_HOMEPAGE

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return UNIPROT_VERSION_STRATEGY

    @property
    def version_check_message(self) -> str:
        return "Checking the UniProt release headers"

    @property
    def validation_message(self) -> str:
        return "Checking that the reviewed records are present in the full dataset"

    @property
    def confirmation_message(self) -> str:
        return "Confirming the UniProt release did not change during download"

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        full = require_download(downloads, UNIPROT_HUMAN_NAME)
        reviewed = require_download(downloads, UNIPROT_REVIEWED_HUMAN_NAME)
        validation = validate_reviewed_in_full(full.resource.path, reviewed.resource.path)
        return SourceValidationResult(
            version=version,
            metadata={
                "version_method": "uniprot_release_headers",
                "validation": {"reviewed_in_full": validation},
            },
        )


class UniProtHumanReferenceProteomeSource(GeneratedSnapshotSource):
    """Retryable paginated export of the Targets human reference proteome."""

    _dataset = DatasetId("uniprot", "human_reference_proteome")
    file_name = UNIPROT_HUMAN_REFERENCE_PROTEOME_NAME
    content_type = "application/gzip"

    def __init__(
        self,
        http: HttpGateway,
        *,
        minimum_records: int = UNIPROT_REFERENCE_PROTEOME_MINIMUM_RECORDS,
        max_page_attempts: int = 5,
        retry_delay_seconds: float = 2.0,
        sleeper: Callable[[float], None] = sleep,
    ):
        super().__init__(http)
        if minimum_records <= 0:
            raise ValueError("minimum_records must be positive")
        if max_page_attempts < 1 or retry_delay_seconds < 0:
            raise ValueError("page attempts must be positive and delay non-negative")
        self._minimum_records = minimum_records
        self._max_page_attempts = max_page_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._sleep = sleeper

    @property
    def dataset(self) -> DatasetId:
        return self._dataset

    @property
    def homepage(self) -> str:
        return UNIPROT_HOMEPAGE

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (UNIPROT_HUMAN_REFERENCE_PROTEOME_URL,)

    @property
    def version_check_description(self) -> str:
        return UNIPROT_VERSION_STRATEGY.description

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return UNIPROT_VERSION_STRATEGY.evidence_urls

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        return UNIPROT_VERSION_STRATEGY.discover(self._http, request)

    def _version_for_fetch(self, request: FetchRequest) -> SourceVersion:
        return self.discover_latest(VersionProbeRequest(request.timeout))

    def _generate(
        self,
        request: FetchRequest,
        version: SourceVersion,
        destination: Path,
    ) -> GeneratedArtifact:
        url: str | None = UNIPROT_HUMAN_REFERENCE_PROTEOME_URL
        visited: set[str] = set()
        pages = 0
        records = 0
        accessions: set[str] = set()
        previous_accession: str | None = None
        wrong_taxon = 0
        expected_total: int | None = None
        with destination.open("wb") as raw_handle:
            with gzip.GzipFile(
                fileobj=raw_handle,
                mode="wb",
                filename="",
                mtime=0,
            ) as zipped:
                with TextIOWrapper(zipped, encoding="utf-8") as handle:
                    handle.write('{"results":[')
                    first = True
                    while url:
                        url = _safe_uniprot_search_url(url)
                        if url in visited:
                            raise SourceValidationError(
                                "UniProt reference-proteome pagination contains a cycle"
                            )
                        visited.add(url)
                        response = self._get_page(url, request)
                        pages += 1
                        page_release = response.metadata.header("X-UniProt-Release")
                        if page_release != version.value:
                            raise SourceValidationError(
                                "UniProt page release does not match the requested release: "
                                f"{page_release!r} != {version.value!r}"
                            )
                        total_header = response.metadata.header("X-Total-Results")
                        if expected_total is None:
                            try:
                                expected_total = int(total_header or "")
                            except ValueError as error:
                                raise SourceValidationError(
                                    "UniProt search response has no valid X-Total-Results"
                                ) from error
                        elif total_header and int(total_header) != expected_total:
                            raise SourceValidationError(
                                "UniProt result count changed during pagination"
                            )
                        payload = response.payload
                        results = payload.get("results") if isinstance(payload, Mapping) else None
                        if not isinstance(results, list):
                            raise SourceValidationError(
                                "UniProt reference-proteome page has no results list"
                            )
                        if not results and records < (expected_total or 0):
                            raise SourceValidationError(
                                "UniProt pagination ended with an empty page before completion"
                            )
                        for item in results:
                            if not isinstance(item, Mapping):
                                raise SourceValidationError(
                                    "UniProt reference-proteome record was not an object"
                                )
                            accession = item.get("primaryAccession")
                            if not isinstance(accession, str) or not accession:
                                raise SourceValidationError(
                                    "UniProt reference-proteome record has no primary accession"
                                )
                            if accession in accessions:
                                raise SourceValidationError(
                                    f"UniProt reference proteome repeats accession {accession}"
                                )
                            if (
                                previous_accession is not None
                                and accession <= previous_accession
                            ):
                                raise SourceValidationError(
                                    "UniProt reference-proteome results are not in "
                                    "the requested accession order"
                                )
                            accessions.add(accession)
                            previous_accession = accession
                            organism = item.get("organism")
                            if (
                                not isinstance(organism, Mapping)
                                or organism.get("taxonId") != 9606
                            ):
                                wrong_taxon += 1
                            if not first:
                                handle.write(",")
                            json.dump(
                                item,
                                handle,
                                ensure_ascii=True,
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                            first = False
                            records += 1
                        request.progress.report(
                            ProgressUpdate(
                                "downloading",
                                f"Downloaded {records:,} UniProt records",
                                records,
                                expected_total,
                            )
                        )
                        url = _next_link(response.metadata.header("Link"))
                    handle.write("]}")

        if expected_total is None or records != expected_total:
            raise SourceValidationError(
                "UniProt reference-proteome pagination was incomplete: "
                f"{records} != {expected_total}"
            )
        confirmed = self.discover_latest(VersionProbeRequest(request.timeout))
        ensure_expected_version(self.dataset, confirmed, version)
        if wrong_taxon:
            raise SourceValidationError(
                f"UniProt reference proteome contains {wrong_taxon} non-human records"
            )
        if records < self._minimum_records:
            raise SourceValidationError(
                "UniProt reference-proteome record count is below the reviewed "
                f"minimum: {records} < {self._minimum_records}"
            )
        return GeneratedArtifact(
            source_url=UNIPROT_HUMAN_REFERENCE_PROTEOME_URL,
            metadata={
                "query": "proteome:UP000005640",
                "taxon_id": 9606,
                "records": records,
                "expected_records": expected_total,
                "pages": pages,
                "page_size": UNIPROT_HUMAN_REFERENCE_PROTEOME_PAGE_SIZE,
                "sort": "accession asc",
                "unique_primary_accessions": len(accessions),
                "minimum_records": self._minimum_records,
                "release_confirmed_after_export": confirmed.value,
            },
        )

    def _get_page(self, url: str, request: FetchRequest) -> HttpJson:
        last_error: SourceAcquisitionError | None = None
        for attempt in range(1, self._max_page_attempts + 1):
            try:
                return self._http.get_json(
                    url,
                    timeout=request.timeout.total_seconds(),
                )
            except SourceAcquisitionError as error:
                last_error = error
                if attempt < self._max_page_attempts:
                    self._sleep(self._retry_delay_seconds * 2 ** (attempt - 1))
        raise SourceAcquisitionError(
            "UniProt reference-proteome page failed after "
            f"{self._max_page_attempts} attempts"
        ) from last_error


def _safe_uniprot_search_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "rest.uniprot.org"
        or parsed.path != "/uniprotkb/search"
    ):
        raise SourceValidationError(f"Unsafe UniProt pagination URL {url!r}")
    return url


def _next_link(value: str | None) -> str | None:
    for part in (value or "").split(","):
        match = re.fullmatch(r'\s*<([^>]+)>;\s*rel="next"\s*', part)
        if match:
            return match.group(1)
    return None


class UniProtHumanIdMappingSource(HttpSnapshotSource):
    """The human UniProt ID mapping table for one observed UniProt release."""

    _dataset = DatasetId("uniprot", "human_idmapping")

    def __init__(
        self,
        http: HttpGateway,
        *,
        minimum_rows: int = UNIPROT_HUMAN_IDMAPPING_MINIMUM_ROWS,
    ):
        super().__init__(http)
        if minimum_rows <= 0:
            raise ValueError("minimum_rows must be positive")
        self._minimum_rows = minimum_rows

    @property
    def dataset(self) -> DatasetId:
        return self._dataset

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return (
            HttpFileSpec(
                UNIPROT_HUMAN_IDMAPPING_URL,
                UNIPROT_HUMAN_IDMAPPING_NAME,
            ),
        )

    @property
    def homepage(self) -> str:
        return UNIPROT_HOMEPAGE

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return UNIPROT_VERSION_STRATEGY

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        downloaded = require_download(downloads, UNIPROT_HUMAN_IDMAPPING_NAME)
        rows = 0
        accessions: set[str] = set()
        databases: set[str] = set()
        try:
            with gzip.open(downloaded.resource.path, "rt", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    fields = line.rstrip("\r\n").split("\t")
                    if len(fields) != 3 or not all(fields):
                        raise SourceValidationError(
                            f"UniProt human ID mapping line {line_number} does not "
                            "contain three non-empty fields"
                        )
                    accession, database, _external_id = fields
                    if not re.fullmatch(r"[A-Z0-9]+(?:-\d+)?", accession):
                        raise SourceValidationError(
                            f"UniProt human ID mapping line {line_number} has invalid "
                            f"accession {accession!r}"
                        )
                    rows += 1
                    accessions.add(accession)
                    databases.add(database)
        except (OSError, UnicodeDecodeError) as error:
            raise SourceValidationError(
                f"Could not parse UniProt human ID mapping: {error}"
            ) from error
        if rows < self._minimum_rows:
            raise SourceValidationError(
                "UniProt human ID mapping row count is below the reviewed minimum: "
                f"{rows} < {self._minimum_rows}"
            )
        return SourceValidationResult(
            version,
            metadata={
                "rows": rows,
                "unique_accessions": len(accessions),
                "database_count": len(databases),
                "minimum_rows": self._minimum_rows,
                "download_last_modified": downloaded.resource.metadata.header(
                    "Last-Modified"
                ),
                "download_etag": downloaded.resource.metadata.header("ETag"),
            },
        )
