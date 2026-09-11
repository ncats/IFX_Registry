"""Reusable PubChem record projections."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import random
import re
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from xml.etree import ElementTree

import requests

from ifx_registry.application.derived_build_models import (
    DerivedRecipeProduct,
    MaterializedRecipeInput,
    ServiceObservation,
)
from ifx_registry.application.ports.derived_builds import DerivedRecipe
from ifx_registry.application.progress import ProgressReporter, ProgressUpdate
from ifx_registry.domain.derived_builds import (
    DerivedRecipeDescriptor,
    RecipeInputSlot,
)
from ifx_registry.domain.errors import InvalidDerivedBuildError
from ifx_registry.domain.models import (
    DatasetId,
    DerivedSnapshotFile,
    ProducerIdentity,
    SnapshotKind,
)

_RECORDS_MANIFEST = "pubchem_compound_records_manifest.tsv"
_OUTPUT_FILE = "cid_molecular_info.tsv"
_RECIPE_DIGEST = hashlib.sha256(b"ifx-registry:pubchem-cid-molecular-info:v1").hexdigest()
_CID_SET_FILE = "pubchem_compound_cids.tsv"
_CID_SET_RECIPE_DIGEST = hashlib.sha256(
    b"ifx-registry:pubchem-compound-cid-set:v1"
).hexdigest()
_COMPOUND_RECORDS_RECIPE_DIGEST = hashlib.sha256(
    b"ifx-registry:pubchem-compound-records:v1"
).hexdigest()
_PUBCHEM_ID_RE = re.compile(
    r"^(?:PUBCHEM\.COMPOUND:|pubchem:)?(?:CID)?(\d+)$",
    re.IGNORECASE,
)
_WIKIPATHWAYS_PUBCHEM_RE = re.compile(
    r"(?:identifiers\.org/pubchem\.compound/|"
    r"rdf\.ncbi\.nlm\.nih\.gov/pubchem/compound/CID)(\d+)"
)


class PubchemCompoundCidSetRecipe(DerivedRecipe):
    @property
    def descriptor(self) -> DerivedRecipeDescriptor:
        return DerivedRecipeDescriptor(
            dataset=DatasetId("pubchem", "compound_cid_set"),
            display_name="PubChem compound CID set",
            description=(
                "Collects the PubChem compound identifiers referenced by registered HMDB, "
                "WikiPathways, LIPID MAPS, and RefMet datasets."
            ),
            revision="1",
            inputs=(
                RecipeInputSlot(
                    "hmdb_metabolites",
                    "HMDB metabolites",
                    SnapshotKind.SOURCE,
                    DatasetId("hmdb", "metabolites_xml"),
                ),
                RecipeInputSlot(
                    "wikipathways_rdf",
                    "WikiPathways RDF",
                    SnapshotKind.SOURCE,
                    DatasetId("wikipathways", "rdf_wp"),
                ),
                RecipeInputSlot(
                    "lipidmaps_structures",
                    "LIPID MAPS structures",
                    SnapshotKind.SOURCE,
                    DatasetId("lipidmaps", "lmsd_sdf"),
                ),
                RecipeInputSlot(
                    "refmet_metabolites",
                    "RefMet metabolites",
                    SnapshotKind.SOURCE,
                    DatasetId("refmet", "metabolites_csv"),
                ),
            ),
            producer=ProducerIdentity(
                "ifx_registry",
                "0.2.0",
                "https://github.com/ncats/IFX_Registry",
                f"sha256:{_CID_SET_RECIPE_DIGEST}",
            ),
            transform={"name": "pubchem_compound_cid_set", "version": 1},
        )

    def build(
        self,
        inputs: Mapping[str, MaterializedRecipeInput],
        destination: Path,
        progress: ProgressReporter,
    ) -> DerivedRecipeProduct:
        progress.report(
            ProgressUpdate("building", "Collecting PubChem identifiers from exact inputs")
        )
        rows: set[tuple[str, str, str, str, str]] = set()
        rows.update(
            _pubchem_rows_from_hmdb(
                _required_input_file(inputs["hmdb_metabolites"], "hmdb_metabolites.zip")
            )
        )
        rows.update(
            _pubchem_rows_from_wikipathways(
                _required_input_file(
                    inputs["wikipathways_rdf"], "wikipathways_rdf_wp.zip"
                )
            )
        )
        rows.update(
            _pubchem_rows_from_lipidmaps(
                _required_input_file(inputs["lipidmaps_structures"], "LMSD.sdf.zip")
            )
        )
        rows.update(
            _pubchem_rows_from_refmet(
                _required_input_file(inputs["refmet_metabolites"], "refmet.csv")
            )
        )
        if not rows:
            raise InvalidDerivedBuildError(
                "Selected inputs contain no recognizable PubChem compound identifiers"
            )
        destination.mkdir(parents=True, exist_ok=True)
        output_path = destination / _CID_SET_FILE
        ordered_rows = list(rows)
        ordered_rows.sort(key=_cid_row_sort_key)
        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(
                [
                    "pubchem_id",
                    "cid",
                    "reported_by_source",
                    "reported_by_source_id",
                    "source_field",
                ]
            )
            writer.writerows(ordered_rows)
        source_counts: dict[str, int] = {}
        for _pubchem_id, _cid, source, _source_id, _field in rows:
            source_counts[source] = source_counts.get(source, 0) + 1
        return DerivedRecipeProduct(
            files=(
                DerivedSnapshotFile(
                    output_path,
                    PurePosixPath(_CID_SET_FILE),
                    "text/tab-separated-values",
                ),
            ),
            validation={
                "row_count": len(rows),
                "distinct_cid_count": len({row[1] for row in rows}),
                "source_counts": source_counts,
            },
        )


@dataclass(frozen=True, slots=True)
class PubchemBatchResult:
    payload: Mapping[str, Any]
    statuses: Mapping[str, tuple[str, str, str]]
    evidence: PubchemServiceEvidence | None = None


@dataclass(frozen=True, slots=True)
class PubchemServiceEvidence:
    first_observed_at: datetime
    last_observed_at: datetime
    request_count: int
    retry_count: int
    http_status_counts: Mapping[str, int]
    worst_throttle: str


@dataclass(frozen=True, slots=True)
class PubchemRetryPolicy:
    max_attempts: int = 5
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    retry_budget_seconds: float = 300.0
    minimum_interval_seconds: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("PubChem max_attempts must be positive")
        if min(
            self.base_backoff_seconds,
            self.max_backoff_seconds,
            self.retry_budget_seconds,
            self.minimum_interval_seconds,
        ) < 0:
            raise ValueError("PubChem retry delays and budget must not be negative")


_DEFAULT_RETRY_POLICY = PubchemRetryPolicy()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _full_jitter(upper: float) -> float:
    return random.uniform(0, upper)


@dataclass(frozen=True, slots=True)
class _RequestAttempt:
    observed_at: datetime
    http_status: str
    throttle: str
    is_retry: bool


class PubchemTransientError(InvalidDerivedBuildError):
    """A bounded PubChem request failed for a potentially temporary reason."""


class PubchemCompoundClient(Protocol):
    def fetch_batch(
        self,
        cids: Sequence[str],
        *,
        timeout: float,
    ) -> PubchemBatchResult: ...


class RequestsPubchemCompoundClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        policy: PubchemRetryPolicy = _DEFAULT_RETRY_POLICY,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utcnow,
        jitter: Callable[[float], float] = _full_jitter,
    ):
        self._session = session or requests.Session()
        self._policy = policy
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._session.headers["User-Agent"] = (
            "IFX-Registry/0.2 (+https://github.com/ncats/IFX_Registry)"
        )

    def fetch_batch(
        self,
        cids: Sequence[str],
        *,
        timeout: float,
    ) -> PubchemBatchResult:
        url = _pubchem_url(cids)
        payload, status_code, evidence = self._request_exact_cids(
            url,
            cids,
            timeout=timeout,
            allow_not_found=True,
        )
        if status_code == 404:
            return self._fetch_individually(
                cids,
                timeout=timeout,
                batch_error="PubChem could not resolve the complete CID batch",
                batch_evidence=evidence,
            )
        if payload is None:
            raise InvalidDerivedBuildError("PubChem returned no compound payload")
        return PubchemBatchResult(
            payload,
            {cid: ("ok", str(status_code), "") for cid in cids},
            evidence,
        )

    def _request_exact_cids(
        self,
        url: str,
        cids: Sequence[str],
        *,
        timeout: float,
        allow_not_found: bool,
    ) -> tuple[Mapping[str, Any] | None, int, PubchemServiceEvidence]:
        attempts: list[_RequestAttempt] = []
        retry_wait = 0.0
        last_error = "unknown PubChem failure"
        for attempt_number in range(1, self._policy.max_attempts + 1):
            try:
                response = self._session.get(url, timeout=timeout)
                observed_at = self._clock()
                status_code = response.status_code
                throttle = _throttle_severity(response.headers.get("X-Throttling-Control"))
                attempts.append(
                    _RequestAttempt(
                        observed_at,
                        str(status_code),
                        throttle,
                        attempt_number > 1,
                    )
                )
                if status_code == 200:
                    try:
                        candidate = response.json()
                    except ValueError as error:
                        last_error = f"PubChem returned invalid JSON: {error}"
                    else:
                        if isinstance(candidate, dict):
                            statuses = _statuses_for_payload(cids, candidate, "200")
                            errors = [
                                value[2]
                                for value in statuses.values()
                                if value[0] == "error"
                            ]
                            if not errors:
                                self._sleep(_throttle_delay(throttle, self._policy))
                                return candidate, status_code, _service_evidence(attempts)
                            last_error = errors[0]
                        else:
                            last_error = "PubChem returned a non-object JSON payload"
                elif status_code == 404 and allow_not_found:
                    self._sleep(_throttle_delay(throttle, self._policy))
                    return None, status_code, _service_evidence(attempts)
                elif status_code == 429 or status_code >= 500:
                    last_error = f"PubChem returned transient HTTP {status_code}"
                else:
                    raise InvalidDerivedBuildError(
                        f"PubChem request failed permanently with HTTP {status_code}: "
                        f"{response.text[:500]}"
                    )
                retry_after = _retry_after_seconds(
                    response.headers.get("Retry-After"),
                    observed_at,
                )
                throttle_delay = _throttle_delay(throttle, self._policy)
            except requests.RequestException as error:
                observed_at = self._clock()
                attempts.append(
                    _RequestAttempt(observed_at, "network_error", "unknown", attempt_number > 1)
                )
                last_error = f"PubChem network request failed: {error}"
                retry_after = 0.0
                throttle_delay = self._policy.minimum_interval_seconds

            if attempt_number >= self._policy.max_attempts:
                break
            backoff_ceiling = min(
                self._policy.max_backoff_seconds,
                self._policy.base_backoff_seconds * (2 ** (attempt_number - 1)),
            )
            delay = max(retry_after, throttle_delay, self._jitter(backoff_ceiling))
            if retry_wait + delay > self._policy.retry_budget_seconds:
                raise PubchemTransientError(
                    f"PubChem retry budget exhausted after {len(attempts)} requests: {last_error}"
                )
            self._sleep(delay)
            retry_wait += delay
        raise PubchemTransientError(
            f"PubChem request failed after {len(attempts)} attempts: {last_error}"
        )

    def _fetch_individually(
        self,
        cids: Sequence[str],
        *,
        timeout: float,
        batch_error: str,
        batch_evidence: PubchemServiceEvidence,
    ) -> PubchemBatchResult:
        compounds: list[object] = []
        statuses: dict[str, tuple[str, str, str]] = {}
        evidences = [batch_evidence]
        for cid in cids:
            url = _pubchem_url((cid,))
            try:
                payload, status_code, evidence = self._request_exact_cids(
                    url,
                    (cid,),
                    timeout=timeout,
                    allow_not_found=True,
                )
                evidences.append(evidence)
                if status_code == 200 and payload is not None:
                    compounds.extend(payload.get("PC_Compounds") or [])
                    statuses[cid] = ("ok", "200", "")
                else:
                    statuses[cid] = (
                        "not_found",
                        "404",
                        f"PubChem compound was not found: {cid}",
                    )
            except PubchemTransientError:
                raise
            except InvalidDerivedBuildError as error:
                statuses[cid] = ("error", "", str(error))
        return PubchemBatchResult(
            {
                "PC_Compounds": compounds,
                "recovered_from_individual_requests": True,
                "batch_error": batch_error,
            },
            statuses,
            _merge_service_evidence(evidences),
        )


class PubchemCompoundRecordsRecipe(DerivedRecipe):
    def __init__(self, client: PubchemCompoundClient | None = None):
        self._client = client or RequestsPubchemCompoundClient()

    @property
    def descriptor(self) -> DerivedRecipeDescriptor:
        return DerivedRecipeDescriptor(
            dataset=DatasetId("pubchem", "compound_records"),
            display_name="PubChem compound records",
            description=(
                "Retrieves canonical PubChem compound records for an exact registered "
                "compound CID set."
            ),
            revision="1",
            inputs=(
                RecipeInputSlot(
                    "compound_cids",
                    "PubChem compound CID set",
                    SnapshotKind.DERIVED,
                    DatasetId("pubchem", "compound_cid_set"),
                ),
            ),
            producer=ProducerIdentity(
                "ifx_registry",
                "0.2.0",
                "https://github.com/ncats/IFX_Registry",
                f"sha256:{_COMPOUND_RECORDS_RECIPE_DIGEST}",
            ),
            transform={
                "name": "pubchem_compound_records",
                "version": 1,
                "batch_size": 100,
                "delay_seconds": 0.25,
                "timeout_seconds": 120,
            },
        )

    def build(
        self,
        inputs: Mapping[str, MaterializedRecipeInput],
        destination: Path,
        progress: ProgressReporter,
    ) -> DerivedRecipeProduct:
        cid_file = _required_input_file(inputs["compound_cids"], _CID_SET_FILE)
        cids = _load_compound_cids(cid_file)
        if not cids:
            raise InvalidDerivedBuildError("The selected compound CID set is empty")
        destination.mkdir(parents=True, exist_ok=True)
        manifest_path = destination / _RECORDS_MANIFEST
        output_files = [
            DerivedSnapshotFile(
                manifest_path,
                PurePosixPath(_RECORDS_MANIFEST),
                "text/tab-separated-values",
            )
        ]
        counts = {"ok": 0, "not_found": 0, "error": 0}
        service_evidence: list[PubchemServiceEvidence] = []
        payload_digests: list[str] = []
        batches = tuple(_chunks(cids, 100))
        with manifest_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(
                [
                    "cid",
                    "pubchem_id",
                    "batch_file",
                    "status",
                    "http_status",
                    "payload_sha256",
                    "error",
                ]
            )
            for batch_number, batch_cids in enumerate(batches, start=1):
                progress.report(
                    ProgressUpdate(
                        "building",
                        "Retrieving PubChem compound records",
                        batch_number,
                        len(batches),
                    )
                )
                result = self._client.fetch_batch(
                    batch_cids,
                    timeout=120,
                )
                if result.evidence is not None:
                    service_evidence.append(result.evidence)
                batch_name = f"pubchem_compound_records_batch_{batch_number:06d}.json.gz"
                batch_path = destination / batch_name
                _write_deterministic_gzip_json(batch_path, result.payload)
                batch_sha = _sha256_file(batch_path)
                payload_digests.append(batch_sha)
                output_files.append(
                    DerivedSnapshotFile(
                        batch_path,
                        PurePosixPath(batch_name),
                        "application/gzip",
                    )
                )
                for cid in batch_cids:
                    status, http_status, error = result.statuses.get(
                        cid,
                        ("error", "", "PubChem client returned no status"),
                    )
                    counts[status] = counts.get(status, 0) + 1
                    writer.writerow(
                        [
                            cid,
                            f"PUBCHEM.COMPOUND:{cid}",
                            batch_name,
                            status,
                            http_status,
                            batch_sha if status == "ok" else "",
                            error,
                        ]
                    )
        if counts["error"]:
            raise InvalidDerivedBuildError(
                f"PubChem retrieval failed for {counts['error']} compounds; "
                "the incomplete result was not registered"
            )
        observations: tuple[ServiceObservation, ...] = ()
        if service_evidence:
            evidence = _merge_service_evidence(service_evidence)
            observations = (
                ServiceObservation(
                    service_id="pubchem:pug_rest",
                    service_name="PubChem PUG REST",
                    interface="https",
                    operation="compound records by CID",
                    endpoint_template=(
                        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
                        "{comma-separated-cids}/JSON"
                    ),
                    first_observed_at=evidence.first_observed_at,
                    last_observed_at=evidence.last_observed_at,
                    request_count=evidence.request_count,
                    retry_count=evidence.retry_count,
                    http_status_counts=evidence.http_status_counts,
                    worst_throttle=evidence.worst_throttle,
                    response_payload_sha256=hashlib.sha256(
                        "\n".join(payload_digests).encode("ascii")
                    ).hexdigest(),
                ),
            )
        return DerivedRecipeProduct(
            files=tuple(output_files),
            validation={
                "requested_cid_count": len(cids),
                "ok_cid_count": counts["ok"],
                "not_found_cid_count": counts["not_found"],
                "error_cid_count": counts["error"],
                "batch_size": 100,
            },
            observations=observations,
        )


def _statuses_for_payload(
    requested_cids: Sequence[str],
    payload: Mapping[str, Any],
    http_status: str,
) -> dict[str, tuple[str, str, str]]:
    requested = set(requested_cids)
    returned = _returned_cids(payload)
    unexpected = returned - requested
    if unexpected:
        detail = ", ".join(sorted(unexpected, key=int)[:10])
        message = f"PubChem response contained unexpected CIDs: {detail}"
        return {cid: ("error", http_status, message) for cid in requested_cids}
    return {
        cid: (
            ("ok", http_status, "")
            if cid in returned
            else ("error", http_status, f"PubChem response omitted requested CID: {cid}")
        )
        for cid in requested_cids
    }


def _write_deterministic_gzip_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    path.write_bytes(gzip.compress(encoded, mtime=0))


def _returned_cids(payload: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    compounds = payload.get("PC_Compounds")
    if not isinstance(compounds, list):
        return result
    for compound in compounds:
        if not isinstance(compound, dict):
            continue
        identifier = compound.get("id")
        if not isinstance(identifier, dict):
            continue
        nested = identifier.get("id")
        if not isinstance(nested, dict):
            continue
        cid = nested.get("cid")
        if isinstance(cid, int) and cid >= 0:
            result.add(str(cid))
    return result


_THROTTLE_RANK: dict[str, int] = {
    "unknown": 0,
    "green": 1,
    "yellow": 2,
    "red": 3,
    "black": 4,
}


def _throttle_severity(header: str | None) -> str:
    if not header:
        return "unknown"
    severities = re.findall(r"\b(green|yellow|red|black)\b", header, re.IGNORECASE)
    if not severities:
        return "unknown"
    normalized: list[str] = [str(item).lower() for item in severities]
    return max(normalized, key=lambda item: _THROTTLE_RANK[item])


def _throttle_delay(severity: str, policy: PubchemRetryPolicy) -> float:
    return max(
        policy.minimum_interval_seconds,
        {"yellow": 1.0, "red": 5.0, "black": 30.0}.get(severity, 0.0),
    )


def _retry_after_seconds(header: str | None, observed_at: datetime) -> float:
    if not header:
        return 0.0
    try:
        return max(0.0, float(header.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(header)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - observed_at).total_seconds())


def _service_evidence(attempts: Sequence[_RequestAttempt]) -> PubchemServiceEvidence:
    status_counts: dict[str, int] = {}
    for attempt in attempts:
        status_counts[attempt.http_status] = status_counts.get(attempt.http_status, 0) + 1
    return PubchemServiceEvidence(
        first_observed_at=min(item.observed_at for item in attempts),
        last_observed_at=max(item.observed_at for item in attempts),
        request_count=len(attempts),
        retry_count=sum(item.is_retry for item in attempts),
        http_status_counts=status_counts,
        worst_throttle=max(
            (item.throttle for item in attempts),
            key=_THROTTLE_RANK.__getitem__,
        ),
    )


def _merge_service_evidence(
    evidences: Sequence[PubchemServiceEvidence],
) -> PubchemServiceEvidence:
    status_counts: dict[str, int] = {}
    for evidence in evidences:
        for status, count in evidence.http_status_counts.items():
            status_counts[status] = status_counts.get(status, 0) + count
    return PubchemServiceEvidence(
        first_observed_at=min(item.first_observed_at for item in evidences),
        last_observed_at=max(item.last_observed_at for item in evidences),
        request_count=sum(item.request_count for item in evidences),
        retry_count=sum(item.retry_count for item in evidences),
        http_status_counts=status_counts,
        worst_throttle=max(
            (item.worst_throttle for item in evidences),
            key=_THROTTLE_RANK.__getitem__,
        ),
    )


class PubchemCidMolecularInfoRecipe(DerivedRecipe):
    @property
    def descriptor(self) -> DerivedRecipeDescriptor:
        return DerivedRecipeDescriptor(
            dataset=DatasetId("pubchem", "cid_molecular_info"),
            display_name="PubChem molecular information",
            description=(
                "Extracts identifiers, structures, formulas, masses, and names from "
                "registered PubChem compound records."
            ),
            revision="1",
            inputs=(
                RecipeInputSlot(
                    "compound_records",
                    "PubChem compound records",
                    SnapshotKind.DERIVED,
                    DatasetId("pubchem", "compound_records"),
                ),
            ),
            producer=ProducerIdentity(
                "ifx_registry",
                "0.2.0",
                "https://github.com/ncats/IFX_Registry",
                f"sha256:{_RECIPE_DIGEST}",
            ),
            transform={"name": "pubchem_cid_molecular_info", "version": 1},
        )

    def build(
        self,
        inputs: Mapping[str, MaterializedRecipeInput],
        destination: Path,
        progress: ProgressReporter,
    ) -> DerivedRecipeProduct:
        records = inputs["compound_records"]
        if records.local_directory is None:
            raise InvalidDerivedBuildError("PubChem compound records must contain files")
        progress.report(
            ProgressUpdate("building", "Extracting molecular information from PubChem records")
        )
        rows = _molecular_info_rows(records.local_directory)
        destination.mkdir(parents=True, exist_ok=True)
        output_path = destination / _OUTPUT_FILE
        fieldnames = (
            "pubchem_id",
            "cid",
            "monoisotopic_mass",
            "inchikey",
            "inchi_key_prefix",
            "molecular_formula",
            "molecular_weight",
            "canonical_smiles",
            "isomeric_smiles",
            "inchi",
            "iupac_name",
        )
        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, delimiter="\t", fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return DerivedRecipeProduct(
            files=(
                DerivedSnapshotFile(
                    output_path,
                    PurePosixPath(_OUTPUT_FILE),
                    "text/tab-separated-values",
                ),
            ),
            validation={
                "row_count": len(rows),
                "with_inchikey_count": sum(1 for row in rows if row.get("inchikey")),
                "with_monoisotopic_mass_count": sum(
                    1 for row in rows if row.get("monoisotopic_mass")
                ),
            },
        )


def _required_input_file(item: MaterializedRecipeInput, name: str) -> Path:
    if item.local_directory is None:
        raise InvalidDerivedBuildError(f"{item.slot.label} must contain registered files")
    path = item.local_directory / name
    if not path.is_file():
        raise InvalidDerivedBuildError(f"{item.slot.label} is missing required file {name}")
    return path


def _pubchem_url(cids: Sequence[str]) -> str:
    return (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
        f"{','.join(cids)}/JSON"
    )


def _load_compound_cids(path: Path) -> list[str]:
    cids: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            cid = (row.get("cid") or "").strip()
            if cid:
                if not cid.isdigit():
                    raise InvalidDerivedBuildError(
                        f"Compound CID set contains a non-numeric CID: {cid}"
                    )
                cids.add(cid)
    return sorted(cids, key=int)


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[start : start + size] for start in range(0, len(values), size)]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _cid_row_sort_key(
    item: tuple[str, str, str, str, str],
) -> tuple[int, str, str, str]:
    return int(item[1]), item[2], item[3], item[4]


def _normalize_pubchem_id(value: object) -> tuple[str, str] | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"NA", "N/A", "NONE", "NULL", "0"}:
        return None
    match = _PUBCHEM_ID_RE.match(text)
    if match is None:
        return None
    cid = match.group(1)
    return cid, f"PUBCHEM.COMPOUND:{cid}"


def _pubchem_rows_from_hmdb(path: Path) -> set[tuple[str, str, str, str, str]]:
    rows: set[tuple[str, str, str, str, str]] = set()
    with zipfile.ZipFile(path) as archive, archive.open("hmdb_metabolites.xml") as handle:
        accession = None
        pubchem_id = None
        for _event, element in ElementTree.iterparse(handle, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "accession":
                accession = (element.text or "").strip()
            elif tag == "pubchem_compound_id":
                pubchem_id = (element.text or "").strip()
            elif tag == "metabolite":
                normalized = _normalize_pubchem_id(pubchem_id)
                if normalized and accession:
                    cid, normalized_id = normalized
                    rows.add(
                        (
                            normalized_id,
                            cid,
                            "hmdb",
                            f"HMDB:{accession}",
                            "pubchem_compound_id",
                        )
                    )
                accession = None
                pubchem_id = None
                element.clear()
    return rows


def _pubchem_rows_from_wikipathways(path: Path) -> set[tuple[str, str, str, str, str]]:
    rows: set[tuple[str, str, str, str, str]] = set()
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if name.endswith("/") or not name.endswith((".ttl", ".rdf", ".nt")):
                continue
            with archive.open(name) as handle:
                for raw_line in handle:
                    line = raw_line.decode("utf-8", errors="ignore")
                    for match in _WIKIPATHWAYS_PUBCHEM_RE.finditer(line):
                        normalized = _normalize_pubchem_id(match.group(1))
                        if normalized is not None:
                            cid, normalized_id = normalized
                            rows.add(
                                (
                                    normalized_id,
                                    cid,
                                    "wikipathways",
                                    "",
                                    "pubchem.compound",
                                )
                            )
    return rows


def _pubchem_rows_from_lipidmaps(path: Path) -> set[tuple[str, str, str, str, str]]:
    rows: set[tuple[str, str, str, str, str]] = set()
    with zipfile.ZipFile(path) as archive, archive.open("structures.sdf") as handle:
        lipidmaps_id = ""
        pending_field = None
        for raw_line in handle:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if pending_field == "LM_ID":
                lipidmaps_id = f"LIPIDMAPS:{line}"
                pending_field = None
                continue
            if pending_field == "PUBCHEM_CID":
                normalized = _normalize_pubchem_id(line)
                if normalized:
                    cid, normalized_id = normalized
                    rows.add(
                        (
                            normalized_id,
                            cid,
                            "lipidmaps",
                            lipidmaps_id,
                            "PUBCHEM_CID",
                        )
                    )
                pending_field = None
                continue
            if line == "> <LM_ID>":
                pending_field = "LM_ID"
            elif line == "> <PUBCHEM_CID>":
                pending_field = "PUBCHEM_CID"
            elif line == "$$$$":
                lipidmaps_id = ""
                pending_field = None
    return rows


def _pubchem_rows_from_refmet(path: Path) -> set[tuple[str, str, str, str, str]]:
    rows: set[tuple[str, str, str, str, str]] = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        reader.fieldnames = [field.strip() for field in reader.fieldnames or []]
        for row in reader:
            clean = {key.strip(): (value or "").strip() for key, value in row.items()}
            normalized = _normalize_pubchem_id(clean.get("pubchem_cid"))
            refmet_id = clean.get("refmet_id")
            if normalized and refmet_id:
                cid, normalized_id = normalized
                rows.add(
                    (
                        normalized_id,
                        cid,
                        "refmet",
                        f"REFMET:{refmet_id}",
                        "pubchem_cid",
                    )
                )
    return rows


def _molecular_info_rows(records_dir: Path) -> list[dict[str, str]]:
    manifest_path = records_dir / _RECORDS_MANIFEST
    if not manifest_path.is_file():
        raise InvalidDerivedBuildError(
            f"PubChem compound-record input is missing {_RECORDS_MANIFEST}"
        )
    with manifest_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        batch_files = sorted(
            {
                row["batch_file"]
                for row in reader
                if row.get("status") == "ok" and row.get("batch_file")
            }
        )
    if not batch_files:
        raise InvalidDerivedBuildError(
            "PubChem compound-record input contains no successful record batches"
        )
    rows: dict[str, dict[str, str]] = {}
    for batch_file in batch_files:
        relative_path = PurePosixPath(batch_file)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise InvalidDerivedBuildError(
                f"PubChem compound-record manifest contains an unsafe batch path: {batch_file}"
            )
        path = records_dir.joinpath(*relative_path.parts)
        if not path.resolve().is_relative_to(records_dir.resolve()):
            raise InvalidDerivedBuildError(
                f"PubChem compound-record manifest contains an unsafe batch path: {batch_file}"
            )
        if not path.is_file():
            raise InvalidDerivedBuildError(
                f"PubChem compound-record input is missing declared batch {batch_file}"
            )
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        for compound in payload.get("PC_Compounds", []) or []:
            row = _molecular_info_row(compound)
            if row["cid"]:
                rows[row["cid"]] = row
    if not rows:
        raise InvalidDerivedBuildError(
            "PubChem compound-record input contains no compounds to project"
        )
    return [rows[cid] for cid in sorted(rows, key=int)]


def _molecular_info_row(compound: Mapping[str, Any]) -> dict[str, str]:
    cid = str(((compound.get("id") or {}).get("id") or {}).get("cid") or "")
    row = {
        "pubchem_id": f"PUBCHEM.COMPOUND:{cid}" if cid else "",
        "cid": cid,
        "monoisotopic_mass": "",
        "inchikey": "",
        "inchi_key_prefix": "",
        "molecular_formula": "",
        "molecular_weight": "",
        "canonical_smiles": "",
        "isomeric_smiles": "",
        "inchi": "",
        "iupac_name": "",
    }
    for prop in compound.get("props", []) or []:
        urn = prop.get("urn") or {}
        label = str(urn.get("label") or "").lower()
        name = str(urn.get("name") or "").lower()
        value = _property_value(prop.get("value") or {})
        if value is None:
            continue
        text = str(value)
        if label == "molecular formula":
            row["molecular_formula"] = text
        elif label == "molecular weight":
            row["molecular_weight"] = text
        elif (label == "weight" and name == "monoisotopic") or (
            label == "mass" and name == "exact"
        ):
            row["monoisotopic_mass"] = text
        elif label == "inchi":
            row["inchi"] = text
        elif label == "inchikey":
            row["inchikey"] = text
            row["inchi_key_prefix"] = text.split("-", 1)[0]
        elif label == "smiles" and name in {"canonical", "connectivity"}:
            row["canonical_smiles"] = text
        elif label == "smiles" and name in {"isomeric", "absolute"}:
            row["isomeric_smiles"] = text
        elif (
            label == "iupac name"
            and name in {"preferred", "allowed", "cas-like style"}
            and not row["iupac_name"]
        ):
            row["iupac_name"] = text
    return row


def _property_value(value: Mapping[str, Any]) -> object | None:
    for key in ("sval", "fval", "ival"):
        if key in value:
            result: object = value[key]
            return result
    return None
