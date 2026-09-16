"""Release-pinned human UniProt isoform exports from the UniProt SPARQL service."""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import sleep
from typing import Any
from urllib.parse import urlencode

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.application.progress import ProgressUpdate
from ifx_registry.application.versioning import ensure_expected_version
from ifx_registry.domain.errors import SourceAcquisitionError, SourceValidationError
from ifx_registry.domain.models import DatasetId, SnapshotFile, SourceSnapshot, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway, HttpJson
from ifx_registry.infrastructure.sources.uniprot import UNIPROT_VERSION_STRATEGY
from ifx_registry.infrastructure.workspace import SnapshotWorkspace

UNIPROT_SPARQL_ENDPOINT = "https://sparql.uniprot.org/sparql/"
UNIPROT_SPARQL_VOID = "https://sparql.uniprot.org/.well-known/void"
UNIPROT_ISOFORM_EXPORT_REVISION = "export1"
UNIPROT_CANONICAL_ISOFORMS_FILE = "uniprot_canonical_isoforms.csv"
UNIPROT_COMPUTATIONAL_ISOFORMS_FILE = (
    "uniprot_computationally_mapped_isoforms.csv"
)
UNIPROT_ISOFORM_COLUMNS = (
    "entry",
    "uniprot_id",
    "isoform",
    "uniprot_sequence",
    "isCanonical",
)

_RELEASE_QUERY = """
SELECT ?version
FROM <https://sparql.uniprot.org/.well-known/void>
WHERE { [] <http://purl.org/pav/version> ?version }
""".strip()

_CANONICAL_QUERY = """
PREFIX taxon: <http://purl.uniprot.org/taxonomy/>
PREFIX up: <http://purl.uniprot.org/core/>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT ?entry ?sequence ?sequenceValue ?isCanonical
WHERE {
  GRAPH <http://sparql.uniprot.org/uniprot> {
    ?entry a up:Protein ; up:organism taxon:9606 ; up:sequence ?sequence .
    ?sequence rdf:value ?sequenceValue .
    OPTIONAL { ?sequence a up:Simple_Sequence . BIND(true AS ?likelyIsCanonical) }
    OPTIONAL {
      FILTER(?likelyIsCanonical)
      ?sequence a up:External_Sequence .
      BIND(true AS ?isComplicated)
    }
    BIND(STRAFTER(STR(?entry), "/uniprot/") AS ?entryAcc)
    BIND(STRAFTER(STR(?sequence), "/isoforms/") AS ?seqId)
    BIND(IF(CONTAINS(?seqId, "-"), STRBEFORE(?seqId, "-"), ?seqId) AS ?seqBase)
    BIND(IF(?isComplicated, ?entryAcc = ?seqBase, ?likelyIsCanonical) AS ?isCanonical)
  }
}
ORDER BY ?entry ?sequence
""".strip()

_COMPUTATIONAL_QUERY = """
PREFIX up: <http://purl.uniprot.org/core/>
PREFIX taxon: <http://purl.uniprot.org/taxonomy/>
SELECT ?entry ?sequence ?isCanonical
WHERE {
  GRAPH <http://sparql.uniprot.org/uniprot> {
    ?entry a up:Protein ; up:organism taxon:9606 ; up:potentialSequence ?sequence .
    OPTIONAL { ?sequence a up:Simple_Sequence . BIND(true AS ?likelyIsCanonical) }
    OPTIONAL {
      FILTER(?likelyIsCanonical)
      ?sequence a up:External_Sequence .
      BIND(true AS ?isComplicated)
    }
    BIND(STRAFTER(STR(?entry), "/uniprot/") AS ?entryAcc)
    BIND(STRAFTER(STR(?sequence), "/isoforms/") AS ?seqId)
    BIND(IF(CONTAINS(?seqId, "-"), STRBEFORE(?seqId, "-"), ?seqId) AS ?seqBase)
    BIND(IF(?isComplicated, ?entryAcc = ?seqBase, ?likelyIsCanonical) AS ?isCanonical)
  }
}
ORDER BY ?entry ?sequence
""".strip()

_COMPUTATIONAL_EXISTS_QUERY = """
PREFIX up: <http://purl.uniprot.org/core/>
PREFIX taxon: <http://purl.uniprot.org/taxonomy/>
ASK {
  GRAPH <http://sparql.uniprot.org/uniprot> {
    ?entry a up:Protein ; up:organism taxon:9606 ; up:potentialSequence ?sequence .
  }
}
""".strip()

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


def _now_utc() -> datetime:
    return datetime.now(UTC)


class UniProtHumanIsoformsSource(SourceAdapter):
    """Generate the two established Targets isoform CSVs from one SPARQL release."""

    _dataset = DatasetId("uniprot", "human_isoforms")

    def __init__(
        self,
        http: HttpGateway,
        *,
        clock: Clock = _now_utc,
        sleeper: Sleeper = sleep,
        max_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
        query_timeout_seconds: float = 300.0,
        minimum_canonical_rows: int = 100_000,
        minimum_computational_rows: int = 1_000,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if retry_delay_seconds < 0 or query_timeout_seconds <= 0:
            raise ValueError("SPARQL delays must be non-negative and timeout positive")
        if minimum_canonical_rows < 1 or minimum_computational_rows < 1:
            raise ValueError("UniProt isoform minimum row counts must be positive")
        self._http = http
        self._clock = clock
        self._sleep = sleeper
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._query_timeout_seconds = query_timeout_seconds
        self._minimum_rows = {
            "canonical": minimum_canonical_rows,
            "computational": minimum_computational_rows,
        }

    @property
    def dataset(self) -> DatasetId:
        return self._dataset

    @property
    def homepage(self) -> str:
        return "https://www.uniprot.org/"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (UNIPROT_SPARQL_ENDPOINT, UNIPROT_SPARQL_VOID)

    @property
    def version_check_description(self) -> str:
        return (
            "Reads the release published by the UniProt SPARQL dataset and confirms "
            "that it matches the current UniProt release headers."
        )

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return (UNIPROT_SPARQL_VOID, *UNIPROT_VERSION_STRATEGY.evidence_urls)

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        sparql_release, response = self._discover_sparql_release(
            request.timeout.total_seconds()
        )
        rest_release = UNIPROT_VERSION_STRATEGY.discover(self._http, request)
        if sparql_release != rest_release.value:
            raise SourceValidationError(
                "UniProt SPARQL and REST releases do not agree: "
                f"{sparql_release} != {rest_release.value}"
            )
        return SourceVersion(
            f"{sparql_release}-{UNIPROT_ISOFORM_EXPORT_REVISION}",
            version_date=rest_release.version_date,
            evidence={
                "method": "uniprot_sparql_void_and_release_headers",
                "sparql_release": sparql_release,
                "sparql_response_url": response.metadata.final_url,
                "rest_release": rest_release.value,
                "rest_release_evidence": dict(rest_release.evidence),
                "export_revision": UNIPROT_ISOFORM_EXPORT_REVISION,
            },
        )

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        request.progress.report(
            ProgressUpdate("checking", "Confirming the UniProt SPARQL release")
        )
        version = self.discover_latest(VersionProbeRequest(request.timeout))
        ensure_expected_version(self.dataset, version, request.expected_version)
        snapshot_dir = (
            request.destination / self.dataset.source / self.dataset.dataset / version.value
        )
        generated: list[tuple[str, str, int, str, bool]] = []
        with SnapshotWorkspace(snapshot_dir) as workspace:
            exports = (
                ("canonical", _CANONICAL_QUERY, UNIPROT_CANONICAL_ISOFORMS_FILE),
                (
                    "computational",
                    _COMPUTATIONAL_QUERY,
                    UNIPROT_COMPUTATIONAL_ISOFORMS_FILE,
                ),
            )
            for index, (label, query, file_name) in enumerate(exports, start=1):
                request.progress.report(
                    ProgressUpdate(
                        "downloading",
                        f"Querying {label} human isoforms",
                        index - 1,
                        len(exports),
                    )
                )
                response = self._query(query, timeout=self._query_timeout_seconds)
                empty_confirmed = False
                if label == "computational" and not _bindings(response.payload):
                    existence = self._query(
                        _COMPUTATIONAL_EXISTS_QUERY,
                        timeout=self._query_timeout_seconds,
                    )
                    if _ask_boolean(existence.payload):
                        raise SourceValidationError(
                            "UniProt computational isoform export was empty even though "
                            "an independent existence query found human records"
                        )
                    empty_confirmed = True
                row_count = self._write_isoforms(
                    response.payload,
                    workspace.staging_path / file_name,
                    minimum_rows=self._minimum_rows[label],
                    label=label,
                    allow_empty=empty_confirmed,
                )
                generated.append(
                    (
                        file_name,
                        response.metadata.final_url,
                        row_count,
                        _digest(query),
                        empty_confirmed,
                    )
                )
            confirmed = self.discover_latest(VersionProbeRequest(request.timeout))
            ensure_expected_version(self.dataset, confirmed, version)
            workspace.commit()

        request.progress.report(
            ProgressUpdate("validating", "Validated both UniProt isoform exports", 2, 2)
        )
        return SourceSnapshot(
            dataset=self.dataset,
            version=version,
            files=tuple(
                SnapshotFile(
                    snapshot_dir / file_name,
                    PurePosixPath(file_name),
                    response_url,
                    "text/csv",
                )
                for file_name, response_url, _rows, _query_digest, _empty in generated
            ),
            downloaded_at=self._clock(),
            homepage=self.homepage,
            upstream_urls=self.upstream_urls,
            metadata={
                "taxon_id": 9606,
                "export_revision": UNIPROT_ISOFORM_EXPORT_REVISION,
                "exports": {
                    file_name: {
                        "rows": rows,
                        "query_sha256": query_digest,
                        "empty_confirmed_by_independent_ask": empty_confirmed,
                    }
                    for file_name, _url, rows, query_digest, empty_confirmed in generated
                },
                "release_confirmed_after_export": confirmed.value,
            },
        )

    def _discover_sparql_release(self, timeout: float) -> tuple[str, HttpJson]:
        response = self._query(_RELEASE_QUERY, timeout=timeout)
        bindings = _bindings(response.payload)
        releases = {
            value
            for row in bindings
            if (value := _binding_value(row, "version", required=False))
        }
        if len(releases) != 1:
            raise SourceValidationError(
                "UniProt SPARQL release query must return exactly one release"
            )
        return next(iter(releases)), response

    def _query(self, query: str, *, timeout: float) -> HttpJson:
        url = f"{UNIPROT_SPARQL_ENDPOINT}?{urlencode({'query': query})}"
        last_error: SourceAcquisitionError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._http.get_json(
                    url,
                    timeout=timeout,
                    headers={"Accept": "application/sparql-results+json"},
                )
            except SourceAcquisitionError as error:
                last_error = error
                if attempt < self._max_attempts:
                    self._sleep(self._retry_delay_seconds * 2 ** (attempt - 1))
        raise SourceAcquisitionError(
            f"UniProt SPARQL query failed after {self._max_attempts} attempts"
        ) from last_error

    @staticmethod
    def _write_isoforms(
        payload: Any,
        destination: Path,
        *,
        minimum_rows: int,
        label: str,
        allow_empty: bool = False,
    ) -> int:
        rows: list[tuple[str, str, str, str, str]] = []
        for binding in _bindings(payload):
            entry = _uri_tail(_binding_value(binding, "entry"))
            sequence = _uri_tail(_binding_value(binding, "sequence"))
            rows.append(
                (
                    entry,
                    sequence,
                    sequence,
                    _binding_value(binding, "sequenceValue", required=False),
                    _binding_value(binding, "isCanonical", required=False) or "0",
                )
            )
        if len(rows) < minimum_rows and not (allow_empty and not rows):
            raise SourceValidationError(
                f"UniProt {label} isoform query returned only {len(rows)} records; "
                f"expected at least {minimum_rows}"
            )
        rows.sort(key=lambda row: (row[0], row[1], row[3], row[4]))
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(UNIPROT_ISOFORM_COLUMNS)
            writer.writerows(rows)
        return len(rows)


def _ask_boolean(payload: Any) -> bool:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("boolean"), bool):
        raise SourceValidationError("UniProt SPARQL ASK response has no boolean result")
    return bool(payload["boolean"])


def _bindings(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise SourceValidationError("UniProt SPARQL response was not an object")
    results = payload.get("results")
    bindings = results.get("bindings") if isinstance(results, Mapping) else None
    if not isinstance(bindings, list) or any(not isinstance(row, Mapping) for row in bindings):
        raise SourceValidationError("UniProt SPARQL response has no valid bindings array")
    return bindings


def _binding_value(
    binding: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
) -> str:
    raw = binding.get(name)
    value = raw.get("value") if isinstance(raw, Mapping) else None
    if isinstance(value, str) and value:
        return value
    if required:
        raise SourceValidationError(f"UniProt SPARQL binding has no {name}")
    return ""


def _uri_tail(value: str) -> str:
    tail = value.rstrip("/").rsplit("/", 1)[-1]
    if not tail:
        raise SourceValidationError(f"UniProt SPARQL returned invalid URI {value!r}")
    return tail


def _digest(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()
