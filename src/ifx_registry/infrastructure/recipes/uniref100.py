"""UniRef100 membership evidence for an exact harmonized protein-ID artifact."""

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

_PROTEIN_INPUT = "protein_ids"
_UNIPROT_INPUT = "uniprot_isoforms"
_PROTEIN_FILE = "protein_ids.tsv"
_OUTPUT_FILE = "uniprot_uniref100_xref.csv"
_ACCESSION_RE = re.compile(r"^[A-Z0-9]+(?:-\d+)?$")
_OUTPUT_COLUMNS = (
    "uniprot_id",
    "uniref100_cluster_id",
    "uniref100_identity",
    "uniref100_is_seed",
    "uniref100_is_representative",
)
_RECIPE_DIGEST = hashlib.sha256(
    b"ifx-registry:uniref100-memberships:v1"
).hexdigest()


class UniRef100MembershipsRecipe(DerivedRecipe):
    """Retrieve UniRef100 support for noncanonical proteins and their parents."""

    def __init__(
        self,
        client: UniProtSparqlClient | None = None,
        *,
        batch_size: int = 25,
        request_timeout: float = 120.0,
        minimum_mapped_accessions: int = 100,
        minimum_mapping_ratio: float = 0.05,
    ):
        if batch_size < 1 or request_timeout <= 0:
            raise ValueError("batch size and request timeout must be positive")
        if minimum_mapped_accessions < 1:
            raise ValueError("minimum mapped accessions must be positive")
        if not 0 < minimum_mapping_ratio <= 1:
            raise ValueError("minimum mapping ratio must be in (0, 1]")
        self._client = client or RequestsUniProtSparqlClient()
        self._batch_size = batch_size
        self._request_timeout = request_timeout
        self._minimum_mapped_accessions = minimum_mapped_accessions
        self._minimum_mapping_ratio = minimum_mapping_ratio

    @property
    def descriptor(self) -> DerivedRecipeDescriptor:
        return DerivedRecipeDescriptor(
            dataset=DatasetId("uniprot", "uniref100_memberships"),
            display_name="UniRef100 Protein Memberships",
            description=(
                "Retrieves UniRef100 membership evidence for noncanonical proteins "
                "and their canonical parents from an exact harmonized protein-ID file."
            ),
            revision="1",
            inputs=(
                RecipeInputSlot(
                    _PROTEIN_INPUT,
                    "Target Graph Protein IDs",
                    SnapshotKind.DERIVED,
                    DatasetId("target_graph", "protein_ids"),
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
                "name": "uniref100_memberships",
                "version": 1,
                "query_scope": "noncanonical_with_canonical_parents",
                "batch_size": self._batch_size,
                "minimum_mapped_accessions": self._minimum_mapped_accessions,
                "minimum_mapping_ratio": self._minimum_mapping_ratio,
            },
        )

    def build(
        self,
        inputs: Mapping[str, MaterializedRecipeInput],
        destination: Path,
        progress: ProgressReporter,
    ) -> DerivedRecipeProduct:
        protein_dir = _required_directory(inputs[_PROTEIN_INPUT])
        expected_release = _release_from_isoform_input(inputs[_UNIPROT_INPUT])
        accessions, source_rows, scoped_rows = _load_accessions(
            protein_dir / _PROTEIN_FILE
        )
        responses: list[SparqlResponse] = []
        release = self._query_release(responses)
        if release != expected_release:
            raise InvalidDerivedBuildError(
                "Selected UniProt isoform snapshot does not match the live SPARQL "
                f"release: {expected_release} != {release}"
            )

        mappings: dict[str, tuple[str, str, bool, bool]] = {}
        batches = [
            accessions[index : index + self._batch_size]
            for index in range(0, len(accessions), self._batch_size)
        ]
        for batch_number, batch in enumerate(batches, start=1):
            progress.report(
                ProgressUpdate(
                    "building",
                    f"Querying UniRef100 memberships ({batch_number}/{len(batches)})",
                    batch_number - 1,
                    len(batches),
                )
            )
            batch_mappings = self._fetch_batch(batch, responses)
            overlap = mappings.keys() & batch_mappings.keys()
            if overlap:
                raise InvalidDerivedBuildError(
                    f"UniRef100 returned duplicate batches for {sorted(overlap)[0]}"
                )
            mappings.update(batch_mappings)

        mapped_count = len(mappings)
        mapping_ratio = mapped_count / len(accessions)
        if (
            mapped_count < min(self._minimum_mapped_accessions, len(accessions))
            or mapping_ratio < self._minimum_mapping_ratio
        ):
            raise InvalidDerivedBuildError(
                "UniRef100 returned implausibly incomplete membership coverage: "
                f"{mapped_count}/{len(accessions)} ({mapping_ratio:.1%})"
            )

        confirmed = self._query_release(responses)
        if confirmed != expected_release:
            raise InvalidDerivedBuildError(
                "UniProt SPARQL release changed while building UniRef100 memberships: "
                f"{expected_release} != {confirmed}"
            )
        destination.mkdir(parents=True, exist_ok=True)
        output = destination / _OUTPUT_FILE
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(_OUTPUT_COLUMNS)
            for accession in accessions:
                cluster, identity, is_seed, is_representative = mappings.get(
                    accession,
                    ("", "", False, False),
                )
                writer.writerow(
                    [accession, cluster, identity, is_seed, is_representative]
                )

        return DerivedRecipeProduct(
            files=(
                DerivedSnapshotFile(output, PurePosixPath(_OUTPUT_FILE), "text/csv"),
            ),
            validation={
                "source_rows": source_rows,
                "scoped_rows": scoped_rows,
                "requested_accessions": len(accessions),
                "mapped_accessions": len(mappings),
                "unmapped_accessions": len(accessions) - len(mappings),
                "mapping_ratio": mapping_ratio,
                "minimum_mapped_accessions": self._minimum_mapped_accessions,
                "minimum_mapping_ratio": self._minimum_mapping_ratio,
                "uniprot_release": expected_release,
                "query_scope": "noncanonical_with_canonical_parents",
            },
            observations=(_observation(responses),),
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
        accessions: Sequence[str],
        responses: list[SparqlResponse],
    ) -> dict[str, tuple[str, str, bool, bool]]:
        try:
            response = self._client.query(
                _membership_query(accessions),
                timeout=self._request_timeout,
            )
        except UniProtSparqlError as error:
            if error.status_code in {400, 413} and len(accessions) > 1:
                midpoint = len(accessions) // 2
                return {
                    **self._fetch_batch(accessions[:midpoint], responses),
                    **self._fetch_batch(accessions[midpoint:], responses),
                }
            raise InvalidDerivedBuildError(
                f"UniRef100 query failed for accession {accessions[0]}"
            ) from error
        responses.append(response)
        requested = set(accessions)
        result: dict[str, tuple[str, str, bool, bool]] = {}
        for binding in sparql_bindings(response.payload):
            accession = _uri_tail(sparql_value(binding, "protein"))
            cluster = _uri_tail(sparql_value(binding, "cluster"))
            if accession not in requested:
                raise InvalidDerivedBuildError(
                    f"UniRef100 returned unrequested accession {accession}"
                )
            value = (
                cluster,
                sparql_value(binding, "identity", required=False),
                sparql_value(binding, "isSeed", required=False).casefold() == "true",
                sparql_value(
                    binding,
                    "isRepresentative",
                    required=False,
                ).casefold()
                == "true",
            )
            previous = result.get(accession)
            if previous is not None and previous != value:
                raise InvalidDerivedBuildError(
                    f"UniRef100 returned conflicting memberships for {accession}"
                )
            result[accession] = value
        return result


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


def _load_accessions(path: Path) -> tuple[list[str], int, int]:
    if not path.is_file():
        raise InvalidDerivedBuildError(f"Protein input is missing {_PROTEIN_FILE}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {
            "uniprot_id",
            "canonical_isoform_status",
            "canonical_ifx_id",
            "ncats_protein_id",
        }
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise InvalidDerivedBuildError(
                f"{_PROTEIN_FILE} is missing UniRef100 scope columns"
            )
        rows = list(reader)
    noncanonical_indexes = {
        index
        for index, row in enumerate(rows)
        if (row.get("canonical_isoform_status") or "").strip() != "canonical"
    }
    parent_ids = {
        (row.get("canonical_ifx_id") or "").strip()
        for index, row in enumerate(rows)
        if index in noncanonical_indexes
        if (row.get("canonical_ifx_id") or "").strip()
    }
    scoped = [
        row
        for index, row in enumerate(rows)
        if index in noncanonical_indexes
        or (row.get("ncats_protein_id") or "").strip() in parent_ids
    ]
    accessions: set[str] = set()
    for row in scoped:
        accession = (row.get("uniprot_id") or "").strip()
        if not accession:
            continue
        if not _ACCESSION_RE.fullmatch(accession):
            raise InvalidDerivedBuildError(
                f"{_PROTEIN_FILE} contains invalid UniProt accession {accession!r}"
            )
        accessions.add(accession)
    if not accessions:
        raise InvalidDerivedBuildError(
            f"{_PROTEIN_FILE} contains no accessions in the configured scope"
        )
    return sorted(accessions), len(rows), len(scoped)


def _membership_query(accessions: Sequence[str]) -> str:
    values = "\n".join(
        f"    <http://purl.uniprot.org/uniprot/{accession}>" for accession in accessions
    )
    return f"""
PREFIX up: <http://purl.uniprot.org/core/>
SELECT ?protein ?cluster ?identity ?isSeed ?isRepresentative
FROM <http://sparql.uniprot.org/uniref>
FROM <http://sparql.uniprot.org/uniprot>
WHERE {{
  VALUES ?protein {{
{values}
  }}
  ?cluster up:member ?member ;
           up:member/up:sequenceFor ?protein ;
           up:identity ?identity .
  FILTER(CONTAINS(STR(?cluster), "UniRef100_"))
  OPTIONAL {{ ?cluster up:seed ?member . BIND("true" AS ?isSeed) }}
  OPTIONAL {{
    ?cluster up:representative ?member .
    BIND("true" AS ?isRepresentative)
  }}
}}
ORDER BY ?protein ?cluster
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
        operation="UniRef100 membership by UniProt accession",
        endpoint_template=UNIPROT_SPARQL_ENDPOINT,
        first_observed_at=min(item.observed_at for item in attempts),
        last_observed_at=max(item.observed_at for item in attempts),
        request_count=request_count,
        retry_count=retry_count,
        http_status_counts=status_counts,
        worst_throttle="unknown",
        response_payload_sha256=combined_digest,
    )
