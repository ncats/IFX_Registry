"""Ensembl transcript to UniProt isoform mappings from exact Registry inputs."""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from ifx_registry.application.derived_build_models import (
    DerivedRecipeProduct,
    MaterializedRecipeInput,
    ServiceObservation,
)
from ifx_registry.application.ports.derived_builds import DerivedRecipe
from ifx_registry.application.progress import ProgressReporter, ProgressUpdate
from ifx_registry.domain.derived_builds import DerivedRecipeDescriptor, RecipeInputSlot
from ifx_registry.domain.errors import InvalidDerivedBuildError
from ifx_registry.domain.models import (
    DatasetId,
    DerivedSnapshotFile,
    ProducerIdentity,
    SnapshotKind,
)
from ifx_registry.infrastructure.uniprot_sparql import (
    UNIPROT_SPARQL_ENDPOINT,
    UNIPROT_SPARQL_RELEASE_QUERY,
    RequestsUniProtSparqlClient,
    SparqlResponse,
    UniProtSparqlClient,
    UniProtSparqlError,
    merge_attempt_counts,
    sparql_bindings,
    sparql_value,
)

_ENSEMBL_INPUT = "ensembl_biomart"
_UNIPROT_INPUT = "uniprot_isoforms"
_ENSEMBL_FILE = "gene_transcript_identifiers.csv"
_OUTPUT_FILE = "ensembl_uniprot_isoform_xrefs.csv"
_TRANSCRIPT_COLUMN = "Transcript stable ID version"
_PEPTIDE_COLUMN = "Protein stable ID version"
_TRANSCRIPT_RE = re.compile(r"^ENST\d+(?:\.\d+)?$")
_RECIPE_DIGEST = hashlib.sha256(
    b"ifx-registry:ensembl-uniprot-isoform-xrefs:v1"
).hexdigest()


class EnsemblUniProtIsoformXrefsRecipe(DerivedRecipe):
    """Query UniProt for the peptide-bearing transcripts in one Ensembl snapshot."""

    def __init__(
        self,
        client: UniProtSparqlClient | None = None,
        *,
        batch_size: int = 50,
        request_timeout: float = 120.0,
        minimum_matched_transcripts: int = 1_000,
        minimum_mapping_ratio: float = 0.01,
    ):
        if batch_size < 1 or request_timeout <= 0:
            raise ValueError("batch size and request timeout must be positive")
        if minimum_matched_transcripts < 1:
            raise ValueError("minimum matched transcripts must be positive")
        if not 0 < minimum_mapping_ratio <= 1:
            raise ValueError("minimum mapping ratio must be in (0, 1]")
        self._client = client or RequestsUniProtSparqlClient()
        self._batch_size = batch_size
        self._request_timeout = request_timeout
        self._minimum_matched_transcripts = minimum_matched_transcripts
        self._minimum_mapping_ratio = minimum_mapping_ratio

    @property
    def descriptor(self) -> DerivedRecipeDescriptor:
        return DerivedRecipeDescriptor(
            dataset=DatasetId("ensembl", "uniprot_isoform_xrefs"),
            display_name="Ensembl to UniProt Isoform Xrefs",
            description=(
                "Queries the pinned UniProt release for isoform mappings of "
                "peptide-bearing transcripts in one exact Ensembl BioMart snapshot."
            ),
            revision="1",
            inputs=(
                RecipeInputSlot(
                    _ENSEMBL_INPUT,
                    "Ensembl Human BioMart",
                    SnapshotKind.SOURCE,
                    DatasetId("ensembl", "human_biomart"),
                ),
                RecipeInputSlot(
                    _UNIPROT_INPUT,
                    "UniProt Human Isoforms",
                    SnapshotKind.SOURCE,
                    DatasetId("uniprot", "human_isoforms"),
                ),
            ),
            producer=ProducerIdentity(
                "ifx_registry",
                "0.2.0",
                "https://github.com/ncats/IFX_Registry",
                f"sha256:{_RECIPE_DIGEST}",
            ),
            transform={
                "name": "ensembl_uniprot_isoform_xrefs",
                "version": 1,
                "scope": "peptide_bearing_transcripts",
                "batch_size": self._batch_size,
                "minimum_matched_transcripts": self._minimum_matched_transcripts,
                "minimum_mapping_ratio": self._minimum_mapping_ratio,
            },
        )

    def build(
        self,
        inputs: Mapping[str, MaterializedRecipeInput],
        destination: Path,
        progress: ProgressReporter,
    ) -> DerivedRecipeProduct:
        ensembl_dir = _required_directory(inputs[_ENSEMBL_INPUT])
        expected_release = _release_from_isoform_input(inputs[_UNIPROT_INPUT])
        transcripts = _load_transcripts(ensembl_dir / _ENSEMBL_FILE)
        responses: list[SparqlResponse] = []
        actual_release = self._query_release(responses)
        if actual_release != expected_release:
            raise InvalidDerivedBuildError(
                "Selected UniProt isoform snapshot does not match the live SPARQL "
                f"release: {expected_release} != {actual_release}"
            )

        rows: set[tuple[str, str]] = set()
        batches = [
            transcripts[index : index + self._batch_size]
            for index in range(0, len(transcripts), self._batch_size)
        ]
        for batch_number, batch in enumerate(batches, start=1):
            progress.report(
                ProgressUpdate(
                    "building",
                    f"Querying UniProt isoforms ({batch_number}/{len(batches)})",
                    batch_number - 1,
                    len(batches),
                )
            )
            rows.update(self._fetch_batch(batch, responses))

        matched = {transcript for transcript, _isoform in rows}
        mapping_ratio = len(matched) / len(transcripts)
        if (
            len(matched) < min(self._minimum_matched_transcripts, len(transcripts))
            or mapping_ratio < self._minimum_mapping_ratio
        ):
            raise InvalidDerivedBuildError(
                "UniProt returned implausibly incomplete Ensembl transcript coverage: "
                f"{len(matched)}/{len(transcripts)} ({mapping_ratio:.1%})"
            )

        confirmed_release = self._query_release(responses)
        if confirmed_release != expected_release:
            raise InvalidDerivedBuildError(
                "UniProt SPARQL release changed while building Ensembl isoform xrefs: "
                f"{expected_release} != {confirmed_release}"
            )
        destination.mkdir(parents=True, exist_ok=True)
        output = destination / _OUTPUT_FILE
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["ensembl_transcript_id_version", "SPARQL_uniprot_isoform"]
            )
            writer.writerows(sorted(rows))

        observations = (_observation(responses),)
        return DerivedRecipeProduct(
            files=(
                DerivedSnapshotFile(
                    output,
                    PurePosixPath(_OUTPUT_FILE),
                    "text/csv",
                ),
            ),
            validation={
                "requested_transcripts": len(transcripts),
                "matched_transcripts": len(matched),
                "mapping_rows": len(rows),
                "mapping_ratio": mapping_ratio,
                "minimum_matched_transcripts": self._minimum_matched_transcripts,
                "minimum_mapping_ratio": self._minimum_mapping_ratio,
                "uniprot_release": expected_release,
                "query_scope": "peptide_bearing_transcripts",
            },
            observations=observations,
        )

    def _query_release(self, responses: list[SparqlResponse]) -> str:
        response = self._client.query(
            UNIPROT_SPARQL_RELEASE_QUERY,
            timeout=self._request_timeout,
        )
        responses.append(response)
        values = {
            sparql_value(binding, "version")
            for binding in sparql_bindings(response.payload)
        }
        if len(values) != 1:
            raise InvalidDerivedBuildError(
                "UniProt SPARQL release query must return exactly one release"
            )
        return next(iter(values))

    def _fetch_batch(
        self,
        transcripts: Sequence[str],
        responses: list[SparqlResponse],
    ) -> set[tuple[str, str]]:
        try:
            response = self._client.query(
                _mapping_query(transcripts),
                timeout=self._request_timeout,
            )
        except UniProtSparqlError as error:
            if error.status_code == 400 and len(transcripts) > 1:
                midpoint = len(transcripts) // 2
                return self._fetch_batch(transcripts[:midpoint], responses) | self._fetch_batch(
                    transcripts[midpoint:], responses
                )
            raise InvalidDerivedBuildError(
                "UniProt isoform query failed for Ensembl transcript batch"
            ) from error
        responses.append(response)
        requested = set(transcripts)
        rows: set[tuple[str, str]] = set()
        for binding in sparql_bindings(response.payload):
            transcript = _uri_tail(sparql_value(binding, "ensemblTranscript"))
            isoform = _uri_tail(sparql_value(binding, "isoform"))
            if transcript not in requested:
                raise InvalidDerivedBuildError(
                    f"UniProt returned unrequested Ensembl transcript {transcript}"
                )
            rows.add((transcript, isoform))
        return rows


def _required_directory(value: MaterializedRecipeInput) -> Path:
    if value.local_directory is None:
        raise InvalidDerivedBuildError(f"Input {value.slot.name} has no files")
    return value.local_directory


def _release_from_isoform_input(value: MaterializedRecipeInput) -> str:
    version = value.reference.ref.version.value
    suffix = "-export1"
    if not version.endswith(suffix) or len(version) == len(suffix):
        raise InvalidDerivedBuildError(
            "UniProt isoform input version must end in -export1"
        )
    return version[: -len(suffix)]


def _load_transcripts(path: Path) -> list[str]:
    if not path.is_file():
        raise InvalidDerivedBuildError(f"Ensembl input is missing {_ENSEMBL_FILE}")
    transcripts: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not {
            _TRANSCRIPT_COLUMN,
            _PEPTIDE_COLUMN,
        }.issubset(reader.fieldnames):
            raise InvalidDerivedBuildError(
                f"{_ENSEMBL_FILE} is missing transcript or peptide columns"
            )
        for row_number, row in enumerate(reader, start=2):
            peptide = (row.get(_PEPTIDE_COLUMN) or "").strip()
            if not peptide:
                continue
            transcript = (row.get(_TRANSCRIPT_COLUMN) or "").strip()
            if not _TRANSCRIPT_RE.fullmatch(transcript):
                raise InvalidDerivedBuildError(
                    f"{_ENSEMBL_FILE} row {row_number} has invalid transcript {transcript!r}"
                )
            transcripts.add(transcript)
    if not transcripts:
        raise InvalidDerivedBuildError(
            f"{_ENSEMBL_FILE} contains no peptide-bearing transcripts"
        )
    return sorted(transcripts)


def _mapping_query(transcripts: Sequence[str]) -> str:
    values = "\n".join(
        f"    ensembltranscript:{transcript}" for transcript in transcripts
    )
    return f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX ensembltranscript: <http://rdf.ebi.ac.uk/resource/ensembl.transcript/>
PREFIX up: <http://purl.uniprot.org/core/>
PREFIX database: <http://purl.uniprot.org/database/>
SELECT ?ensemblTranscript ?isoform
WHERE {{
  VALUES ?ensemblTranscript {{
{values}
  }}
  GRAPH <http://sparql.uniprot.org/uniprot> {{
    {{
      ?ensemblTranscript up:database database:Ensembl ; rdfs:seeAlso ?isoform .
    }}
    UNION
    {{
      ?entry up:database database:Ensembl ;
             rdfs:seeAlso ?ensemblTranscript ;
             up:sequence ?isoform .
      OPTIONAL {{ ?ensemblTranscript rdfs:seeAlso ?isoformSpecific . }}
      FILTER (!BOUND(?isoformSpecific))
    }}
  }}
}}
ORDER BY ?ensemblTranscript ?isoform
""".strip()


def _uri_tail(value: str) -> str:
    result = value.rstrip("/").rsplit("/", 1)[-1]
    if not result:
        raise InvalidDerivedBuildError(f"UniProt SPARQL returned invalid URI {value!r}")
    return result


def _observation(responses: Sequence[SparqlResponse]) -> ServiceObservation:
    attempts = [attempt for response in responses for attempt in response.attempts]
    if not attempts:
        raise InvalidDerivedBuildError("UniProt SPARQL produced no service observations")
    request_count, retry_count, status_counts = merge_attempt_counts(responses)
    combined_digest = hashlib.sha256(
        "\n".join(response.payload_sha256 for response in responses).encode("ascii")
    ).hexdigest()
    return ServiceObservation(
        service_id="uniprot:sparql",
        service_name="UniProt SPARQL",
        interface="sparql",
        operation="Ensembl transcript to UniProt isoform mappings",
        endpoint_template=UNIPROT_SPARQL_ENDPOINT,
        first_observed_at=min(item.observed_at for item in attempts),
        last_observed_at=max(item.observed_at for item in attempts),
        request_count=request_count,
        retry_count=retry_count,
        http_status_counts=status_counts,
        worst_throttle="unknown",
        response_payload_sha256=combined_digest,
    )
