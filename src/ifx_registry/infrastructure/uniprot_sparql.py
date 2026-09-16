"""Retrying UniProt SPARQL transport shared by derived Registry recipes."""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import requests

UNIPROT_SPARQL_ENDPOINT = "https://sparql.uniprot.org/sparql/"
UNIPROT_SPARQL_RELEASE_QUERY = """
SELECT ?version
FROM <https://sparql.uniprot.org/.well-known/void>
WHERE { [] <http://purl.org/pav/version> ?version }
""".strip()


@dataclass(frozen=True, slots=True)
class SparqlAttempt:
    observed_at: datetime
    http_status: str
    is_retry: bool


@dataclass(frozen=True, slots=True)
class SparqlResponse:
    payload: Mapping[str, Any]
    attempts: tuple[SparqlAttempt, ...]
    payload_sha256: str


class UniProtSparqlError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class UniProtSparqlClient(Protocol):
    def query(self, sparql: str, *, timeout: float) -> SparqlResponse: ...


class RequestsUniProtSparqlClient:
    """Form-POST SPARQL JSON client with bounded transient retries."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        max_attempts: int = 3,
        base_backoff_seconds: float = 1.0,
        minimum_interval_seconds: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        jitter: Callable[[float, float], float] = random.uniform,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if base_backoff_seconds < 0 or minimum_interval_seconds < 0:
            raise ValueError("SPARQL delays must not be negative")
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = (
            "IFX-Registry/0.2 (+https://github.com/ncats/IFX_Registry)"
        )
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds
        self._minimum_interval_seconds = minimum_interval_seconds
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._has_queried = False

    def query(self, sparql: str, *, timeout: float) -> SparqlResponse:
        attempts: list[SparqlAttempt] = []
        last_error: BaseException | None = None
        if self._has_queried and self._minimum_interval_seconds:
            self._sleep(self._minimum_interval_seconds)
        self._has_queried = True
        for attempt_number in range(1, self._max_attempts + 1):
            observed_at = self._clock()
            try:
                response = self._session.post(
                    UNIPROT_SPARQL_ENDPOINT,
                    data={"query": sparql},
                    headers={"Accept": "application/sparql-results+json"},
                    timeout=timeout,
                )
                status = response.status_code
                attempts.append(
                    SparqlAttempt(observed_at, str(status), attempt_number > 1)
                )
                if status == 200:
                    try:
                        payload = response.json()
                    except ValueError as error:
                        raise UniProtSparqlError(
                            "UniProt SPARQL returned invalid JSON",
                            status_code=status,
                        ) from error
                    if not isinstance(payload, Mapping):
                        raise UniProtSparqlError(
                            "UniProt SPARQL response was not an object",
                            status_code=status,
                        )
                    encoded = json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    return SparqlResponse(
                        payload,
                        tuple(attempts),
                        hashlib.sha256(encoded).hexdigest(),
                    )
                response_error = UniProtSparqlError(
                    f"UniProt SPARQL returned HTTP {status}",
                    status_code=status,
                )
                if status not in {429, 500, 502, 503, 504}:
                    raise response_error
                last_error = response_error
            except UniProtSparqlError:
                raise
            except requests.RequestException as error:
                attempts.append(
                    SparqlAttempt(observed_at, "transport_error", attempt_number > 1)
                )
                last_error = error
            if attempt_number < self._max_attempts:
                upper = self._base_backoff_seconds * 2 ** (attempt_number - 1)
                self._sleep(self._jitter(0.0, upper))
        raise UniProtSparqlError(
            f"UniProt SPARQL failed after {self._max_attempts} attempts",
            status_code=(
                last_error.status_code
                if isinstance(last_error, UniProtSparqlError)
                else None
            ),
        ) from last_error


def sparql_bindings(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    results = payload.get("results")
    bindings = results.get("bindings") if isinstance(results, Mapping) else None
    if not isinstance(bindings, list) or any(not isinstance(row, Mapping) for row in bindings):
        raise UniProtSparqlError("UniProt SPARQL response has no valid bindings array")
    return bindings


def sparql_value(
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
        raise UniProtSparqlError(f"UniProt SPARQL binding has no {name}")
    return ""


def merge_attempt_counts(responses: Sequence[SparqlResponse]) -> tuple[int, int, dict[str, int]]:
    statuses: dict[str, int] = {}
    attempts = [attempt for response in responses for attempt in response.attempts]
    for attempt in attempts:
        statuses[attempt.http_status] = statuses.get(attempt.http_status, 0) + 1
    return len(attempts), sum(item.is_retry for item in attempts), statuses
