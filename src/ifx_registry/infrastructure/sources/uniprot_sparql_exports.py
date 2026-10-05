"""Independent human UniProt SPARQL exports with release and row-count guards."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import sleep
from typing import Any
from urllib.parse import urlencode

import ijson

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.domain.errors import SourceAcquisitionError, SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway, HttpMetadata
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
)
from ifx_registry.infrastructure.sources.uniprot import UNIPROT_VERSION_STRATEGY
from ifx_registry.infrastructure.sources.uniprot_isoforms import (
    UNIPROT_SPARQL_ENDPOINT,
    UniProtHumanIsoformsSource,
    _binding_value,
    _bindings,
)
from ifx_registry.infrastructure.sources.version_strategies import SourceVersionStrategy
from ifx_registry.infrastructure.workspace import SnapshotWorkspace

EXPORT_REVISION = "export1"

PREFIXES = """PREFIX up: <http://purl.uniprot.org/core/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX taxon: <http://purl.uniprot.org/taxonomy/>
PREFIX database: <http://purl.uniprot.org/database/>
"""
UNIREF_BODY = """
  { ?protein a up:Protein ; up:organism taxon:9606 . }
  UNION {
    ?parent a up:Protein ; up:organism taxon:9606 ; up:sequence ?isoform .
    BIND(IRI(CONCAT("http://purl.uniprot.org/uniprot/",
         STRAFTER(STR(?isoform), "/isoforms/"))) AS ?protein)
  }
  ?member up:sequenceFor ?protein .
  ?cluster up:member ?member ; up:identity ?identity .
  FILTER(STRSTARTS(STR(?cluster), "http://purl.uniprot.org/uniref/UniRef100_"))
  OPTIONAL { ?member up:seedFor ?cluster . BIND(true AS ?seed) }
  OPTIONAL { ?protein up:representativeFor ?cluster . BIND(true AS ?representative) }
  BIND(COALESCE(?seed, false) AS ?isSeed)
  BIND(COALESCE(?representative, false) AS ?isRepresentative)
"""
ENSEMBL_BODY = """
  GRAPH <http://sparql.uniprot.org/uniprot> {
    ?protein a up:Protein ; up:organism taxon:9606 ; rdfs:seeAlso ?ensemblTranscript .
    ?ensemblTranscript up:database database:Ensembl .
    ?ensemblTranscript rdfs:seeAlso ?isoform .
    FILTER(STRSTARTS(STR(?ensemblTranscript),
                    "http://rdf.ebi.ac.uk/resource/ensembl.transcript/ENST"))
    FILTER(REGEX(STR(?isoform), "/isoforms/[A-Z0-9]+-[0-9]+$"))
  }
"""


@dataclass(frozen=True)
class ExportDefinition:
    dataset: str
    filename: str
    variables: tuple[str, ...]
    columns: tuple[str, ...]
    body: str
    minimum_rows: int
    graphs: tuple[str, ...] = ()

    @property
    def dataset_clause(self) -> str:
        return " ".join(f"FROM <{graph}>" for graph in self.graphs)

    @property
    def select(self) -> str:
        return "SELECT DISTINCT " + " ".join("?" + name for name in self.variables)

    @property
    def query(self) -> str:
        return PREFIXES + self.select + " " + self.dataset_clause + " WHERE {\n" + self.body + "}\n"

    @property
    def count_query(self) -> str:
        return (
            PREFIXES
            + "SELECT (COUNT(*) AS ?count) "
            + self.dataset_clause
            + " WHERE { { "
            + self.select
            + " WHERE {\n"
            + self.body
            + "} } }"
        )


UNIREF = ExportDefinition(
    "human_uniref100_sparql",
    "uniprot_uniref100_xref.csv",
    ("protein", "cluster", "identity", "isSeed", "isRepresentative"),
    (
        "uniprot_id",
        "uniref100_cluster_id",
        "uniref100_identity",
        "uniref100_is_seed",
        "uniref100_is_representative",
    ),
    UNIREF_BODY,
    10_000,
    ("http://sparql.uniprot.org/uniprot", "http://sparql.uniprot.org/uniref"),
)
ENSEMBL = ExportDefinition(
    "human_ensembl_isoform_xrefs_sparql",
    "ensembl_uniprot_isoform_xref.csv",
    ("ensemblTranscript", "isoform"),
    ("ensembl_transcript_id_version", "SPARQL_uniprot_isoform"),
    ENSEMBL_BODY,
    10_000,
)


class UniProtSparqlReleaseStrategy(SourceVersionStrategy):
    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return (
            "https://sparql.uniprot.org/.well-known/void",
            *UNIPROT_VERSION_STRATEGY.evidence_urls,
        )

    @property
    def description(self) -> str:
        return "Confirms matching UniProt SPARQL and REST releases, before and after export."

    def discover(self, http: HttpGateway, request: VersionProbeRequest) -> SourceVersion:
        release = UniProtHumanIsoformsSource(http).discover_latest(request)
        # These query contracts evolve independently of the older isoform source.
        return SourceVersion(
            f"{release.evidence['sparql_release']}-{EXPORT_REVISION}",
            version_date=release.version_date,
            evidence={**release.evidence, "export_revision": EXPORT_REVISION},
        )


def query_url(query: str) -> str:
    return UNIPROT_SPARQL_ENDPOINT + "?" + urlencode({"query": query, "format": "json"})


class UniProtSparqlExportSource(HttpSnapshotSource):
    def __init__(
        self,
        http: HttpGateway,
        definition: ExportDefinition,
        *,
        query_timeout_seconds: float = 300,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = sleep,
    ):
        super().__init__(http)
        if query_timeout_seconds <= 0 or max_attempts < 1:
            raise ValueError("Query timeout and retry count must be positive")
        self.definition = definition
        self._query_timeout_seconds = query_timeout_seconds
        self._max_attempts = max_attempts
        self._sleep = sleeper

    @property
    def expected_file_count(self) -> int:
        return 4

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("uniprot", self.definition.dataset)

    @property
    def homepage(self) -> str:
        return "https://sparql.uniprot.org/"

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return UniProtSparqlReleaseStrategy()

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return (
            HttpFileSpec(query_url(self.definition.query), "response.json"),
            HttpFileSpec(query_url(self.definition.count_query), "count.json"),
            # The same response is serialized into the consumer CSV during validation.
            # Keep the original JSON as well, for source-faithful auditability.
        )

    def _download_files(
        self,
        request: FetchRequest,
        workspace: SnapshotWorkspace,
        file_specs: tuple[HttpFileSpec, ...],
    ) -> tuple[DownloadedSourceFile, ...]:
        # SPARQL aggregation can take longer than a small HTTP-file request.
        query_request = replace(request, timeout=timedelta(seconds=self._query_timeout_seconds))
        for attempt in range(self._max_attempts):
            try:
                downloads = super()._download_files(query_request, workspace, file_specs)
                break
            except SourceAcquisitionError:
                if attempt + 1 == self._max_attempts:
                    raise
                self._sleep(2.0 ** (attempt + 1))
        rows = sorted(self._read_rows(downloads[0].resource.path))
        csv_path = workspace.staging_path / self.definition.filename
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.definition.columns)
            writer.writerows(rows)
        query_path = workspace.staging_path / "query.rq"
        query_path.write_text(self.definition.query, encoding="utf-8")
        return downloads + tuple(
            DownloadedSourceFile(
                HttpFileSpec(file_specs[0].url, path.name),
                DownloadedResource(path, HttpMetadata(file_specs[0].url, {"content-type": mime})),
            )
            for path, mime in ((csv_path, "text/csv"), (query_path, "application/sparql-query"))
        )

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        response, count = downloads[:2]
        try:
            count_payload = json.loads(count.resource.path.read_text())
        except (ValueError, UnicodeError) as error:
            raise SourceValidationError("UniProt SPARQL returned invalid JSON") from error
        count_rows = _bindings(count_payload)
        if len(count_rows) != 1:
            raise SourceValidationError("UniProt SPARQL count must contain exactly one row")
        raw_count = _binding_value(count_rows[0], "count")
        if not raw_count.isdigit():
            raise SourceValidationError("UniProt SPARQL returned an invalid count")
        expected = int(raw_count)
        rows = list(self._read_rows(response.resource.path))
        if len(rows) != expected or len(set(rows)) != expected:
            raise SourceValidationError("UniProt SPARQL export/count mismatch or duplicate rows")
        if len(rows) < self.definition.minimum_rows:
            raise SourceValidationError(f"UniProt SPARQL export too small: {len(rows)} rows")
        return SourceValidationResult(
            version,
            {
                "taxon_id": 9606,
                "rows": len(rows),
                "upstream_count": expected,
                "query": self.definition.query,
                "count_query": self.definition.count_query,
                "query_sha256": hashlib.sha256(self.definition.query.encode()).hexdigest(),
                "export_revision": EXPORT_REVISION,
            },
        )

    def _read_rows(self, path: Path) -> Iterator[tuple[str, ...]]:
        # Full human JSON exports can be hundreds of MB. Stream bindings instead
        # of holding the entire response object graph in memory.
        try:
            with path.open("rb") as handle:
                for binding in ijson.items(handle, "results.bindings.item"):
                    if not isinstance(binding, Mapping):
                        raise SourceValidationError("Invalid UniProt SPARQL binding")
                    yield self._row(binding)
        except (ijson.JSONError, UnicodeError) as error:
            raise SourceValidationError("UniProt SPARQL returned invalid JSON") from error

    def _row(self, binding: Mapping[str, Any]) -> tuple[str, ...]:
        values = tuple(_binding_value(binding, name) for name in self.definition.variables)
        if self.definition.dataset == UNIREF.dataset:
            protein = _identifier(
                values[0], "http://purl.uniprot.org/uniprot/", r"[A-Z0-9]+(?:-[0-9]+)?"
            )
            cluster = _identifier(
                values[1], "http://purl.uniprot.org/uniref/", r"UniRef100_[A-Z0-9_-]+"
            )
            try:
                if Decimal(values[2]) != 1:
                    raise SourceValidationError("UniRef100 identity must be 1")
            except InvalidOperation as error:
                raise SourceValidationError("Invalid UniRef identity") from error
            flags = []
            for value in values[3:]:
                if value not in {"true", "false", "1", "0"}:
                    raise SourceValidationError("Invalid UniRef boolean")
                flags.append("True" if value in {"true", "1"} else "False")
            return (protein, cluster, values[2], *flags)
        return (
            _identifier(
                values[0],
                "http://rdf.ebi.ac.uk/resource/ensembl.transcript/",
                r"ENST[0-9]+(?:\.[0-9]+)?",
            ),
            _identifier(values[1], "http://purl.uniprot.org/isoforms/", r"[A-Z0-9]+-[0-9]+"),
        )


def _identifier(value: str, prefix: str, pattern: str) -> str:
    if not value.startswith(prefix) or not re.fullmatch(pattern, value[len(prefix) :]):
        raise SourceValidationError(f"Unexpected UniProt SPARQL identifier: {value!r}")
    return value[len(prefix) :]


class UniProtHumanUniRef100Source(UniProtSparqlExportSource):
    def __init__(self, http: HttpGateway):
        super().__init__(http, UNIREF)


class UniProtHumanEnsemblIsoformXrefsSource(UniProtSparqlExportSource):
    def __init__(self, http: HttpGateway):
        super().__init__(http, ENSEMBL)
