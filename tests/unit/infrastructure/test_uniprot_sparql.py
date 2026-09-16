from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

from ifx_registry.infrastructure.uniprot_sparql import (
    RequestsUniProtSparqlClient,
    UniProtSparqlError,
)


class FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses) -> None:
        self.headers: dict[str, str] = {}
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _clock() -> datetime:
    return datetime(2026, 9, 15, tzinfo=UTC)


def test_retries_transient_status_and_records_attempts() -> None:
    session = FakeSession(
        [
            FakeResponse(503),
            FakeResponse(200, {"results": {"bindings": []}}),
        ]
    )
    delays: list[float] = []
    client = RequestsUniProtSparqlClient(
        session,
        base_backoff_seconds=2,
        minimum_interval_seconds=0,
        sleep=delays.append,
        clock=_clock,
        jitter=lambda _lower, upper: upper,
    )

    response = client.query("SELECT * WHERE {}", timeout=12)

    assert [attempt.http_status for attempt in response.attempts] == ["503", "200"]
    assert [attempt.is_retry for attempt in response.attempts] == [False, True]
    assert delays == [2]
    assert len(session.calls) == 2
    assert session.calls[0]["timeout"] == 12


def test_retries_transport_error() -> None:
    session = FakeSession(
        [
            requests.ConnectionError("temporary"),
            FakeResponse(200, {"results": {"bindings": []}}),
        ]
    )
    client = RequestsUniProtSparqlClient(
        session,
        minimum_interval_seconds=0,
        sleep=lambda _delay: None,
        clock=_clock,
        jitter=lambda _lower, _upper: 0,
    )

    response = client.query("ASK {}", timeout=3)

    assert [attempt.http_status for attempt in response.attempts] == [
        "transport_error",
        "200",
    ]


def test_exhausted_http_retries_preserve_last_status() -> None:
    session = FakeSession([FakeResponse(503), FakeResponse(503), FakeResponse(503)])
    client = RequestsUniProtSparqlClient(
        session,
        minimum_interval_seconds=0,
        sleep=lambda _delay: None,
        clock=_clock,
        jitter=lambda _lower, _upper: 0,
    )

    with pytest.raises(UniProtSparqlError) as error:
        client.query("ASK {}", timeout=3)

    assert error.value.status_code == 503


def test_fails_immediately_for_nonretryable_status() -> None:
    session = FakeSession([FakeResponse(400)])
    client = RequestsUniProtSparqlClient(
        session,
        minimum_interval_seconds=0,
        sleep=lambda _delay: None,
        clock=_clock,
    )

    with pytest.raises(UniProtSparqlError, match="HTTP 400") as error:
        client.query("bad query", timeout=3)

    assert error.value.status_code == 400
    assert len(session.calls) == 1


def test_rejects_invalid_success_payload() -> None:
    session = FakeSession([FakeResponse(200, ["not", "an", "object"])])
    client = RequestsUniProtSparqlClient(
        session,
        minimum_interval_seconds=0,
        sleep=lambda _delay: None,
        clock=_clock,
    )

    with pytest.raises(UniProtSparqlError, match="not an object"):
        client.query("ASK {}", timeout=3)
