"""Tests for shared HTTP download behavior."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest
import requests

from ifx_registry.domain.errors import SourceAcquisitionError
from ifx_registry.infrastructure.http import RequestsHttpGateway


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status_error: Exception | None = None,
        json_payload: Any = None,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        stream_error: Exception | None = None,
    ):
        self.url = "https://cdn.example.org/data.tsv"
        self.headers = headers or {"Content-Type": "text/tab-separated-values"}
        self.status_code = status_code
        self.text = "release-1"
        self._chunks = chunks
        self._status_error = status_error
        self._json_payload = json_payload
        self._stream_error = stream_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error

    def iter_content(self, *, chunk_size: int) -> Iterator[bytes]:
        assert chunk_size == 4
        yield from self._chunks
        if self._stream_error:
            raise self._stream_error

    def json(self) -> Any:
        return self._json_payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeSession:
    def __init__(self, response: FakeResponse | list[FakeResponse]):
        self.responses = response if isinstance(response, list) else [response]
        self.headers: dict[str, str] = {}
        self.last_get: tuple[str, dict[str, Any]] | None = None
        self.last_post: tuple[str, dict[str, Any]] | None = None

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.last_get = (url, kwargs)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def head(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.responses[0]

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.last_post = (url, kwargs)
        return self.responses[0]


def _gateway(response: FakeResponse) -> RequestsHttpGateway:
    session = cast(requests.Session, FakeSession(response))
    return RequestsHttpGateway(session, chunk_size=4)


def test_download_streams_to_atomic_final_path(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "data.tsv"

    downloaded = _gateway(FakeResponse([b"abc", b"", b"def"])).download(
        "https://example.org/data.tsv",
        destination,
        timeout=10,
    )

    assert downloaded.path.read_bytes() == b"abcdef"
    assert downloaded.metadata.final_url == "https://cdn.example.org/data.tsv"
    assert downloaded.metadata.header("content-type") == "text/tab-separated-values"
    assert not destination.with_name("data.tsv.part").exists()


def test_download_rejects_empty_response_and_removes_partial_file(tmp_path: Path) -> None:
    destination = tmp_path / "data.tsv"

    with pytest.raises(SourceAcquisitionError, match="empty response"):
        _gateway(FakeResponse([])).download(
            "https://example.org/data.tsv",
            destination,
            timeout=10,
        )

    assert not destination.exists()
    assert not destination.with_name("data.tsv.part").exists()


def test_download_wraps_http_errors(tmp_path: Path) -> None:
    error = requests.HTTPError("503 Server Error")

    with pytest.raises(SourceAcquisitionError, match="Could not download"):
        _gateway(FakeResponse([], status_error=error)).download(
            "https://example.org/data.tsv",
            tmp_path / "data.tsv",
            timeout=10,
        )


def test_resumable_download_continues_verified_partial(tmp_path: Path) -> None:
    interrupted = FakeResponse(
        [b"abcd"],
        headers={"ETag": '"stable"', "Content-Length": "8"},
        stream_error=requests.exceptions.ChunkedEncodingError("disconnected"),
    )
    resumed = FakeResponse(
        [b"efgh"],
        status_code=206,
        headers={
            "ETag": '"stable"',
            "Content-Range": "bytes 4-7/8",
            "Content-Length": "4",
        },
    )
    session = FakeSession([interrupted, resumed])
    gateway = RequestsHttpGateway(
        cast(requests.Session, session),
        chunk_size=4,
        resumable_retry_delay_seconds=0,
    )
    destination = tmp_path / "large.data"

    result = gateway.download_resumable(
        "https://example.org/large.data",
        destination,
        timeout=10,
        expected_size=8,
    )

    assert result.path.read_bytes() == b"abcdefgh"
    assert session.last_get is not None
    assert session.last_get[1]["headers"] == {
        "Range": "bytes=4-",
        "If-Range": '"stable"',
    }
    assert not destination.with_name("large.data.part").exists()


def test_resumable_download_restarts_when_server_ignores_range(tmp_path: Path) -> None:
    interrupted = FakeResponse(
        [b"abcd"],
        headers={"ETag": '"stable"'},
        stream_error=requests.exceptions.ChunkedEncodingError("disconnected"),
    )
    restarted = FakeResponse(
        [b"abcdefgh"],
        status_code=200,
        headers={"ETag": '"changed"', "Content-Length": "8"},
    )
    session = FakeSession([interrupted, restarted])
    gateway = RequestsHttpGateway(
        cast(requests.Session, session),
        chunk_size=4,
        resumable_retry_delay_seconds=0,
    )
    destination = tmp_path / "large.data"

    gateway.download_resumable(
        "https://example.org/large.data",
        destination,
        timeout=10,
        expected_size=8,
    )

    assert destination.read_bytes() == b"abcdefgh"


def test_resumable_download_discards_orphaned_partial_from_previous_call(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "large.data"
    destination.with_name("large.data.part").write_bytes(b"stale")
    response = FakeResponse(
        [b"fresh-data"],
        headers={"ETag": '"current"', "Content-Length": "10"},
    )
    session = FakeSession(response)
    gateway = RequestsHttpGateway(
        cast(requests.Session, session),
        chunk_size=4,
        resumable_retry_delay_seconds=0,
    )

    gateway.download_resumable(
        "https://example.org/large.data",
        destination,
        timeout=10,
        expected_size=10,
    )

    assert destination.read_bytes() == b"fresh-data"
    assert session.last_get is not None
    assert session.last_get[1]["headers"] is None
    assert not destination.with_name("large.data.part").exists()


def test_gateway_sets_identifiable_user_agent() -> None:
    session = FakeSession(FakeResponse([b"data"]))

    RequestsHttpGateway(cast(requests.Session, session))

    assert session.headers["User-Agent"].startswith("IFX-Registry/")


def test_json_get_decodes_payload_and_preserves_metadata() -> None:
    response = FakeResponse([], json_payload={"version": "42"})

    result = _gateway(response).get_json("https://example.org/data.json", timeout=10)

    assert result.payload == {"version": "42"}
    assert result.metadata.final_url == "https://cdn.example.org/data.tsv"


def test_json_get_sends_additional_request_headers() -> None:
    response = FakeResponse([], json_payload={"count": 0})
    session = FakeSession(response)
    gateway = RequestsHttpGateway(cast(requests.Session, session), chunk_size=4)

    gateway.get_json(
        "https://example.org/reports",
        timeout=10,
        headers={"X-API-Key": "test-key"},
    )

    assert session.last_get is not None
    assert session.last_get[1]["headers"] == {
        "Accept": "application/json",
        "X-API-Key": "test-key",
    }


def test_json_post_sends_payload() -> None:
    response = FakeResponse([], json_payload={"list_id": "abc"})
    session = FakeSession(response)
    gateway = RequestsHttpGateway(cast(requests.Session, session), chunk_size=4)

    result = gateway.post_json("https://example.org/search", {"term": "human"}, timeout=10)

    assert result.payload == {"list_id": "abc"}
    assert session.last_post is not None
    assert session.last_post[1]["json"] == {"term": "human"}


def test_post_download_streams_to_atomic_final_path(tmp_path: Path) -> None:
    response = FakeResponse([b"abc", b"def"])
    session = FakeSession(response)
    gateway = RequestsHttpGateway(cast(requests.Session, session), chunk_size=4)
    destination = tmp_path / "data.csv"

    result = gateway.post_download(
        "https://example.org/export",
        {"id": "cache-42"},
        destination,
        timeout=10,
        accept="text/csv",
    )

    assert result.path.read_bytes() == b"abcdef"
    assert session.last_post is not None
    assert session.last_post[1]["json"] == {"id": "cache-42"}
    assert session.last_post[1]["headers"] == {"Accept": "text/csv"}
