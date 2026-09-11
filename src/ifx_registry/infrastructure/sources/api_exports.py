"""Generated snapshots acquired from JSON APIs and HTML listings."""

from __future__ import annotations

import csv
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.application.progress import ProgressUpdate
from ifx_registry.application.versioning import ensure_expected_version
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SnapshotFile, SourceSnapshot, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.workspace import SnapshotWorkspace

Clock = Callable[[], datetime]


def _now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class GeneratedArtifact:
    """Description and validation metadata for one generated snapshot file."""

    source_url: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    upstream_urls: tuple[str, ...] = ()


class GeneratedSnapshotSource(SourceAdapter, ABC):
    """Template method for sources that generate one file from API responses."""

    file_name: str
    content_type: str

    def __init__(self, http: HttpGateway, clock: Clock = _now_utc):
        self._http = http
        self._clock = clock

    @abstractmethod
    def _version_for_fetch(self, request: FetchRequest) -> SourceVersion:
        """Resolve the exact version that the generated file will represent."""

    @abstractmethod
    def _generate(
        self,
        request: FetchRequest,
        version: SourceVersion,
        destination: Path,
    ) -> GeneratedArtifact:
        """Generate and validate the staged artifact."""

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        request.progress.report(
            ProgressUpdate("checking", f"Resolving the {self.dataset} snapshot version")
        )
        version = self._version_for_fetch(request)
        ensure_expected_version(self.dataset, version, request.expected_version)
        snapshot_dir = (
            request.destination / self.dataset.source / self.dataset.dataset / version.value
        )
        with SnapshotWorkspace(snapshot_dir) as workspace:
            request.progress.report(
                ProgressUpdate("downloading", f"Building {self.file_name}", 1, 1)
            )
            staged_path = workspace.staging_path / self.file_name
            artifact = self._generate(request, version, staged_path)
            if not staged_path.is_file() or staged_path.stat().st_size == 0:
                raise SourceValidationError(
                    f"{self.dataset} generated an empty {self.file_name}"
                )
            workspace.commit()

        return SourceSnapshot(
            dataset=self.dataset,
            version=version,
            files=(
                SnapshotFile(
                    local_path=snapshot_dir / self.file_name,
                    relative_path=PurePosixPath(self.file_name),
                    source_url=artifact.source_url,
                    content_type=self.content_type,
                ),
            ),
            downloaded_at=self._clock(),
            homepage=self.homepage,
            upstream_urls=tuple(
                dict.fromkeys((*self.upstream_urls, *artifact.upstream_urls))
            ),
            metadata=artifact.metadata,
        )


class RequestedCaptureSource(GeneratedSnapshotSource, ABC):
    """A source whose version identifies a Registry capture, not an upstream release."""

    capture_prefix = "capture"

    @abstractmethod
    def _probe_upstream(self, request: VersionProbeRequest) -> None:
        """Confirm that the live endpoint can produce a valid capture."""

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        self._probe_upstream(request)
        captured_at = self._clock().replace(microsecond=0)
        value = f"{self.capture_prefix}_{captured_at.strftime('%Y%m%dT%H%M%SZ')}"
        return SourceVersion(
            value,
            version_date=captured_at.date(),
            discovered_at=captured_at,
            evidence={"type": "requested_capture", "captured_at": captured_at.isoformat()},
        )

    def _version_for_fetch(self, request: FetchRequest) -> SourceVersion:
        # Version checks reserve a human-readable capture ID. Acquisition uses that
        # exact ID rather than pretending the endpoint exposes a stable release.
        if request.expected_version is None:
            return self.discover_latest(VersionProbeRequest(timeout=request.timeout))
        value = request.expected_version.value
        match = re.fullmatch(
            rf"{re.escape(self.capture_prefix)}_(\d{{8}}T\d{{6}}Z)",
            value,
        )
        if match is None:
            raise SourceValidationError(
                f"{self.dataset} capture version must match "
                f"{self.capture_prefix}_YYYYMMDDTHHMMSSZ"
            )
        captured_at = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=UTC
        )
        version_date = captured_at.date()
        if request.expected_version.version_date == version_date:
            return request.expected_version
        return SourceVersion(
            value,
            version_date=version_date,
            discovered_at=captured_at,
            evidence={"type": "requested_capture", "captured_at": captured_at.isoformat()},
        )


CURE_REPORTS_URL = "https://cure-api.ncats.io/v2/reports"
CURE_FIRST_PAGE = f"{CURE_REPORTS_URL}?{urlencode({'limit': 100, 'sort': 'latest'})}"


class CureCaseReportsSource(RequestedCaptureSource):
    """Export all CURE ID case reports as newline-delimited JSON."""

    file_name = "case_reports.jsonl"
    content_type = "application/x-ndjson"
    capture_prefix = "reports"

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("cure", "case_reports")

    @property
    def homepage(self) -> str:
        return "https://cure.ncats.io/"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (CURE_REPORTS_URL,)

    @property
    def version_check_description(self) -> str:
        return (
            "Reserves a UTC timestamp for a complete paginated export; "
            "the CURE API does not expose a release identifier."
        )

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return (CURE_REPORTS_URL,)

    def _probe_upstream(self, request: VersionProbeRequest) -> None:
        response = self._http.get_json(
            CURE_FIRST_PAGE,
            timeout=request.timeout.total_seconds(),
        )
        _cure_results(_require_mapping(response.payload, "CURE reports response"))

    def _generate(
        self,
        request: FetchRequest,
        version: SourceVersion,
        destination: Path,
    ) -> GeneratedArtifact:
        del version
        next_url: str | None = CURE_FIRST_PAGE
        requested_urls: list[str] = []
        page_count = 0
        total_written = 0
        expected_count: int | None = None
        visited: set[str] = set()
        with destination.open("w", encoding="utf-8") as handle:
            while next_url:
                next_url = _safe_cure_page_url(next_url)
                if next_url in visited:
                    raise SourceValidationError("CURE reports pagination contains a cycle")
                if len(visited) >= 10_000:
                    raise SourceValidationError("CURE reports pagination exceeded 10,000 pages")
                visited.add(next_url)
                requested_urls.append(next_url)
                response = self._http.get_json(
                    next_url,
                    timeout=request.timeout.total_seconds(),
                )
                payload = _require_mapping(response.payload, "CURE reports response")
                page_count += 1
                if expected_count is None and isinstance(payload.get("count"), int):
                    expected_count = int(payload["count"])
                results = _cure_results(payload)
                for item in results:
                    if not isinstance(item, Mapping):
                        raise SourceValidationError("CURE reports contained a non-object item")
                    handle.write(json.dumps(item, ensure_ascii=True))
                    handle.write("\n")
                    total_written += 1
                request.progress.report(
                    ProgressUpdate(
                        "downloading",
                        f"Downloaded {total_written} CURE reports",
                        total_written,
                        expected_count,
                    )
                )
                next_value = payload.get("next")
                next_url = (
                    urljoin(next_url, next_value)
                    if isinstance(next_value, str) and next_value
                    else None
                )
        if total_written == 0:
            raise SourceValidationError("CURE reports export returned no records")
        if expected_count is not None and total_written != expected_count:
            raise SourceValidationError(
                f"CURE reports expected {expected_count} records but exported {total_written}"
            )
        return GeneratedArtifact(
            source_url=CURE_REPORTS_URL,
            upstream_urls=tuple(requested_urls),
            metadata={
                "record_count": total_written,
                "page_count": page_count,
                "expected_count": expected_count,
                "version_method": "requested_capture_timestamp",
            },
        )


GLYGEN_SEARCH_URL = "https://api.glygen.org/protein/search_simple/"
GLYGEN_LIST_URL = "https://api.glygen.org/protein/list/"
GLYGEN_DOWNLOAD_URL = "https://api.glygen.org/data/list_download/"
GLYGEN_SEARCH_PAYLOAD = {"term_category": "organism", "term": "human"}


class GlyGenProteinsSource(GeneratedSnapshotSource):
    """Download the current GlyGen human protein list."""

    file_name = "glygen_proteins.csv"
    content_type = "text/csv"

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("glygen", "proteins")

    @property
    def homepage(self) -> str:
        return "https://glygen.org/"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (GLYGEN_SEARCH_URL, GLYGEN_LIST_URL, GLYGEN_DOWNLOAD_URL)

    @property
    def version_check_description(self) -> str:
        return "Uses the stable listcache_id returned by the GlyGen protein-list API."

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return (GLYGEN_SEARCH_URL, GLYGEN_LIST_URL)

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        search = _require_mapping(
            self._http.post_json(
                GLYGEN_SEARCH_URL,
                GLYGEN_SEARCH_PAYLOAD,
                timeout=request.timeout.total_seconds(),
            ).payload,
            "GlyGen search response",
        )
        list_id = search.get("list_id")
        if not isinstance(list_id, str) or not list_id.strip():
            raise SourceValidationError("GlyGen search response did not include list_id")
        listing = _require_mapping(
            self._http.post_json(
                GLYGEN_LIST_URL,
                {"id": list_id},
                timeout=request.timeout.total_seconds(),
            ).payload,
            "GlyGen list response",
        )
        cache_info = listing.get("cache_info")
        cache = cache_info if isinstance(cache_info, Mapping) else {}
        raw_version = cache.get("listcache_id") or listing.get("listcache_id") or list_id
        version = str(raw_version).strip()
        if not version:
            raise SourceValidationError("GlyGen did not provide a usable listcache_id")
        return SourceVersion(
            version,
            evidence={
                "type": "glygen_listcache_id",
                "list_id": list_id,
                "listcache_id": version,
                "record_count": search.get("resultcount"),
            },
        )

    def _version_for_fetch(self, request: FetchRequest) -> SourceVersion:
        return self.discover_latest(VersionProbeRequest(timeout=request.timeout))

    def _generate(
        self,
        request: FetchRequest,
        version: SourceVersion,
        destination: Path,
    ) -> GeneratedArtifact:
        downloaded = self._http.post_download(
            GLYGEN_DOWNLOAD_URL,
            {
                "id": version.value,
                "download_type": "protein_list",
                "format": "csv",
                "compressed": False,
            },
            destination,
            timeout=request.timeout.total_seconds(),
            accept="text/csv",
        )
        content_type = (downloaded.metadata.header("content-type") or "").casefold()
        if content_type and not any(
            allowed in content_type
            for allowed in ("text/csv", "application/csv", "application/octet-stream")
        ):
            raise SourceValidationError(
                f"GlyGen protein download returned unexpected content type {content_type!r}"
            )
        with downloaded.path.open(encoding="utf-8", errors="replace") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            if len(header) < 2 or any(not name.strip() for name in header):
                raise SourceValidationError("GlyGen protein download has an invalid CSV header")
            record_count = sum(1 for row in reader if any(value.strip() for value in row))
        if record_count == 0:
            raise SourceValidationError("GlyGen protein download contained no records")
        return GeneratedArtifact(
            source_url=downloaded.metadata.final_url,
            metadata={
                "record_count": record_count,
                "version_method": "glygen_listcache_id",
                **version.evidence,
            },
        )


class _DarkKinomeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href", "") or ""
        match = re.search(r"/kinase/([^/?#]+)", href)
        if match:
            symbol = unquote(match.group(1)).strip()
            if symbol:
                self.rows.append(
                    {
                        "symbol": symbol,
                        "url": f"https://darkkinome.org/kinase/{quote(symbol)}",
                    }
                )


class DarkKinomeSource(RequestedCaptureSource):
    file_name = "dark_kinome_kinases.tsv"
    content_type = "text/tab-separated-values"
    _url = "https://darkkinome.org/data"

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("dark_kinome", "kinases")

    @property
    def homepage(self) -> str:
        return "https://darkkinome.org/"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (self._url,)

    @property
    def version_check_description(self) -> str:
        return "Uses the Registry capture date because the site exposes no release ID."

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return (self._url,)

    def _probe_upstream(self, request: VersionProbeRequest) -> None:
        response = self._http.get_text(self._url, timeout=request.timeout.total_seconds())
        if not _dark_kinome_rows(response.text):
            raise SourceValidationError("No Dark Kinome kinase links were found")

    def _generate(
        self,
        request: FetchRequest,
        version: SourceVersion,
        destination: Path,
    ) -> GeneratedArtifact:
        del version
        response = self._http.get_text(self._url, timeout=request.timeout.total_seconds())
        rows = _dark_kinome_rows(response.text)
        if not rows:
            raise SourceValidationError("No Dark Kinome kinase links were found")
        _write_tsv(destination, ("symbol", "url"), rows)
        return GeneratedArtifact(
            response.metadata.final_url,
            {"record_count": len(rows), "version_method": "requested_capture_date"},
        )


RESOLUTE_URL = "https://re-solute.eu/api/graphql"
RESOLUTE_QUERY = """
query LinkoutGenes {
  genesList(first: 1000, condition: {isSlc: true}) {
    symbol
    proteinsList {
      nextprotac
      identifiersList { identifier }
    }
  }
}
""".strip()


class ResoluteGenesSource(RequestedCaptureSource):
    file_name = "resolute_genes.tsv"
    content_type = "text/tab-separated-values"

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("resolute", "genes")

    @property
    def homepage(self) -> str:
        return "https://re-solute.eu/"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (RESOLUTE_URL,)

    @property
    def version_check_description(self) -> str:
        return "Uses the Registry capture date because the GraphQL API exposes no release ID."

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return (RESOLUTE_URL,)

    def _probe_upstream(self, request: VersionProbeRequest) -> None:
        response = self._http.post_json(
            RESOLUTE_URL,
            {"query": RESOLUTE_QUERY},
            timeout=request.timeout.total_seconds(),
        )
        _resolute_genes(response.payload)

    def _generate(
        self,
        request: FetchRequest,
        version: SourceVersion,
        destination: Path,
    ) -> GeneratedArtifact:
        del version
        response = self._http.post_json(
            RESOLUTE_URL,
            {"query": RESOLUTE_QUERY},
            timeout=request.timeout.total_seconds(),
        )
        genes = _resolute_genes(response.payload)
        rows: list[dict[str, str]] = []
        genes_with_linkout_ids = 0
        for raw_gene in genes:
            gene = _require_mapping(raw_gene, "RESOLUTE gene")
            symbol = gene.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                continue
            nextprot_ids: set[str] = set()
            ensembl_ids: set[str] = set()
            proteins = gene.get("proteinsList")
            if proteins is None:
                proteins = []
            if not isinstance(proteins, list):
                raise SourceValidationError("RESOLUTE proteinsList was not a list")
            for raw_protein in proteins:
                protein = _require_mapping(raw_protein, "RESOLUTE protein")
                nextprot = protein.get("nextprotac")
                if nextprot is not None and not isinstance(nextprot, str):
                    raise SourceValidationError("RESOLUTE nextprotac was not a string")
                if nextprot:
                    nextprot_ids.add(nextprot)
                identifiers = protein.get("identifiersList")
                if identifiers is None:
                    identifiers = []
                if not isinstance(identifiers, list):
                    raise SourceValidationError(
                        "RESOLUTE identifiersList was not a list"
                    )
                for raw_identifier in identifiers:
                    identifier = _require_mapping(
                        raw_identifier,
                        "RESOLUTE protein identifier",
                    ).get("identifier")
                    if identifier is not None and not isinstance(identifier, str):
                        raise SourceValidationError(
                            "RESOLUTE protein identifier was not a string"
                        )
                    if identifier and identifier.startswith("ENSP"):
                        ensembl_ids.add(identifier)
            if nextprot_ids or ensembl_ids:
                genes_with_linkout_ids += 1
            rows.append(
                {
                    "symbol": symbol,
                    "nextprot_ids": "|".join(sorted(nextprot_ids)),
                    "ensembl_protein_ids": "|".join(sorted(ensembl_ids)),
                    "url": f"https://re-solute.eu/knowledgebase/gene/{quote(symbol)}",
                }
            )
        if not rows:
            raise SourceValidationError("No RESOLUTE genes were returned")
        if genes_with_linkout_ids < max(1, len(rows) // 2):
            raise SourceValidationError(
                "Fewer than half of RESOLUTE genes retained NextProt or Ensembl "
                "protein identifiers"
            )
        _write_tsv(
            destination,
            ("symbol", "nextprot_ids", "ensembl_protein_ids", "url"),
            rows,
        )
        return GeneratedArtifact(
            response.metadata.final_url,
            {
                "record_count": len(rows),
                "genes_with_linkout_ids": genes_with_linkout_ids,
                "linkout_id_coverage": genes_with_linkout_ids / len(rows),
                "version_method": "requested_capture_date",
                "query": RESOLUTE_QUERY,
            },
        )


class LinkedOmicsGenesSource(RequestedCaptureSource):
    file_name = "linkedomics_genes.tsv"
    content_type = "text/tab-separated-values"
    _url = "https://kb.linkedomics.org/data/list/gene"

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("linkedomics", "genes")

    @property
    def homepage(self) -> str:
        return "https://kb.linkedomics.org/"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (self._url,)

    @property
    def version_check_description(self) -> str:
        return "Uses the Registry capture date because the API exposes no release ID."

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return (self._url,)

    def _probe_upstream(self, request: VersionProbeRequest) -> None:
        response = self._http.get_json(self._url, timeout=request.timeout.total_seconds())
        _linkedomics_symbols(response.payload)

    def _generate(
        self,
        request: FetchRequest,
        version: SourceVersion,
        destination: Path,
    ) -> GeneratedArtifact:
        del version
        response = self._http.get_json(self._url, timeout=request.timeout.total_seconds())
        symbols = _linkedomics_symbols(response.payload)
        rows = [
            {
                "symbol": symbol,
                "url": f"https://kb.linkedomics.org/gene/{quote(symbol)}",
            }
            for symbol in symbols
        ]
        rows = _deduplicate_rows(rows, "symbol")
        if not rows:
            raise SourceValidationError("No LinkedOmics genes were returned")
        _write_tsv(destination, ("symbol", "url"), rows)
        return GeneratedArtifact(
            response.metadata.final_url,
            {"record_count": len(rows), "version_method": "requested_capture_date"},
        )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceValidationError(f"{label} was not a JSON object")
    return value


def _cure_results(payload: Mapping[str, Any]) -> list[Any]:
    results = payload.get("results")
    if not isinstance(results, list):
        raise SourceValidationError(
            "CURE reports response did not contain a list in 'results'"
        )
    return results


def _safe_cure_page_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "cure-api.ncats.io":
        raise SourceValidationError("CURE reports pagination returned an unsafe next URL")
    return value


def _dark_kinome_rows(html: str) -> list[dict[str, str]]:
    parser = _DarkKinomeParser()
    parser.feed(html)
    return _deduplicate_rows(parser.rows, "symbol")


def _resolute_genes(payload_value: Any) -> list[Any]:
    payload = _require_mapping(payload_value, "RESOLUTE GraphQL response")
    if payload.get("errors"):
        raise SourceValidationError(
            f"RESOLUTE GraphQL errors: {json.dumps(payload['errors'], sort_keys=True)}"
        )
    data = _require_mapping(payload.get("data"), "RESOLUTE GraphQL data")
    genes = data.get("genesList")
    if not isinstance(genes, list) or not genes:
        raise SourceValidationError("RESOLUTE response did not contain genesList")
    return genes


def _linkedomics_symbols(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        raise SourceValidationError("LinkedOmics gene-list response was not a list")
    if any(not isinstance(symbol, str) for symbol in payload):
        raise SourceValidationError("LinkedOmics gene list contained a non-string symbol")
    symbols = [symbol.strip() for symbol in payload if symbol.strip()]
    if not symbols:
        raise SourceValidationError("No LinkedOmics genes were returned")
    return symbols


def _deduplicate_rows(
    rows: Iterable[dict[str, str]],
    key: str,
) -> list[dict[str, str]]:
    deduplicated: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        value = row[key]
        if value not in seen:
            seen.add(value)
            deduplicated.append(row)
    return deduplicated


def _write_tsv(
    path: Path,
    fieldnames: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
