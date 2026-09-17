"""Shared HTTP boundary used by source adapters."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Any

import requests

from ifx_registry.domain.errors import SourceAcquisitionError

DEFAULT_USER_AGENT = "IFX-Registry/0.1 (+https://github.com/ncats/IFX_Registry)"


@dataclass(frozen=True, slots=True)
class HttpMetadata:
    """Transport metadata retained after an HTTP operation."""

    final_url: str
    headers: Mapping[str, str]

    def header(self, name: str) -> str | None:
        """Look up a response header without relying on header casing."""
        expected = name.casefold()
        return next(
            (value for key, value in self.headers.items() if key.casefold() == expected),
            None,
        )


@dataclass(frozen=True, slots=True)
class HttpText:
    """Text response plus the metadata used to obtain it."""

    text: str
    metadata: HttpMetadata


@dataclass(frozen=True, slots=True)
class DownloadedResource:
    """A completed HTTP download and its response metadata."""

    path: Path
    metadata: HttpMetadata


@dataclass(frozen=True, slots=True)
class HttpJson:
    """Decoded JSON response plus transport metadata."""

    payload: Any
    metadata: HttpMetadata


class HttpGateway(ABC):
    """Small injectable HTTP interface needed by Registry source adapters."""

    @abstractmethod
    def get_text(self, url: str, *, timeout: float) -> HttpText:
        """Read a small text resource."""

    @abstractmethod
    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        """Read response metadata without downloading the response body."""

    @abstractmethod
    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        """Stream a resource to an exact destination path."""

    def download_resumable(
        self,
        url: str,
        destination: Path,
        *,
        timeout: float,
        expected_size: int,
    ) -> DownloadedResource:
        """Download a declared-size resource, resuming when supported.

        Gateways without range support retain the ordinary atomic-download
        behavior. Sources must opt into resumability explicitly.
        """
        return self.download(url, destination, timeout=timeout)

    def get_json(
        self,
        url: str,
        *,
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> HttpJson:
        """Read JSON when supported by the concrete HTTP adapter."""
        del headers
        raise SourceAcquisitionError("This HTTP adapter does not support JSON GET requests")

    def post_json(self, url: str, payload: Mapping[str, Any], *, timeout: float) -> HttpJson:
        """Post JSON and decode JSON when supported by the concrete adapter."""
        raise SourceAcquisitionError("This HTTP adapter does not support JSON POST requests")

    def post_download(
        self,
        url: str,
        payload: Mapping[str, Any],
        destination: Path,
        *,
        timeout: float,
        accept: str | None = None,
    ) -> DownloadedResource:
        """Post JSON and stream the response to a file when supported."""
        raise SourceAcquisitionError("This HTTP adapter does not support POST downloads")


class RequestsHttpGateway(HttpGateway):
    """Requests-backed HTTP gateway with atomic, non-empty downloads."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        chunk_size: int = 1024 * 1024,
        user_agent: str = DEFAULT_USER_AGENT,
        resumable_attempts: int = 5,
        resumable_retry_delay_seconds: float = 2.0,
    ):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if not user_agent.strip():
            raise ValueError("user_agent must not be blank")
        if resumable_attempts < 1:
            raise ValueError("resumable_attempts must be positive")
        if resumable_retry_delay_seconds < 0:
            raise ValueError("resumable_retry_delay_seconds must not be negative")
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._chunk_size = chunk_size
        self._resumable_attempts = resumable_attempts
        self._resumable_retry_delay_seconds = resumable_retry_delay_seconds

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        try:
            with self._session.get(url, timeout=timeout) as response:
                response.raise_for_status()
                return HttpText(response.text, self._metadata(response))
        except requests.RequestException as error:
            raise SourceAcquisitionError(f"Could not read {url}: {error}") from error

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        try:
            with self._session.head(url, timeout=timeout, allow_redirects=True) as response:
                response.raise_for_status()
                return self._metadata(response)
        except requests.RequestException as error:
            raise SourceAcquisitionError(f"Could not read metadata for {url}: {error}") from error

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        destination = Path(destination)
        partial_path = destination.with_name(f"{destination.name}.part")
        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            with self._session.get(url, timeout=timeout, stream=True) as response:
                return self._stream_to_file(response, destination, partial_path, url)
        except SourceAcquisitionError:
            partial_path.unlink(missing_ok=True)
            raise
        except (OSError, requests.RequestException) as error:
            partial_path.unlink(missing_ok=True)
            raise SourceAcquisitionError(f"Could not download {url}: {error}") from error

    def download_resumable(
        self,
        url: str,
        destination: Path,
        *,
        timeout: float,
        expected_size: int,
    ) -> DownloadedResource:
        """Retry one large download and resume a verified partial response."""
        if expected_size <= 0:
            raise ValueError("expected_size must be positive")
        destination = Path(destination)
        partial_path = destination.with_name(f"{destination.name}.part")
        destination.parent.mkdir(parents=True, exist_ok=True)
        validator: str | None = None
        last_error: BaseException | None = None

        for attempt in range(1, self._resumable_attempts + 1):
            offset = partial_path.stat().st_size if partial_path.is_file() else 0
            if offset and validator is None:
                # A partial inherited from outside this active call has no
                # trustworthy identity. Restart it instead of appending.
                partial_path.unlink(missing_ok=True)
                offset = 0
            headers: dict[str, str] = {}
            if offset:
                assert validator is not None
                headers["Range"] = f"bytes={offset}-"
                headers["If-Range"] = validator
            try:
                with self._session.get(
                    url,
                    timeout=timeout,
                    stream=True,
                    headers=headers or None,
                ) as response:
                    response.raise_for_status()
                    metadata = self._metadata(response)
                    response_validator = _resume_validator(metadata)
                    append = offset > 0 and response.status_code == 206
                    if append:
                        try:
                            _validate_content_range(
                                metadata.header("Content-Range"),
                                offset=offset,
                                expected_size=expected_size,
                            )
                            if response_validator != validator:
                                raise SourceAcquisitionError(
                                    f"Resume validator changed while downloading {url}"
                                )
                        except SourceAcquisitionError:
                            partial_path.unlink(missing_ok=True)
                            validator = None
                            raise
                    else:
                        offset = 0
                        partial_path.unlink(missing_ok=True)
                    validator = response_validator
                    mode = "ab" if append else "wb"
                    with partial_path.open(mode) as handle:
                        for chunk in response.iter_content(chunk_size=self._chunk_size):
                            if chunk:
                                handle.write(chunk)

                observed_size = partial_path.stat().st_size
                if observed_size != expected_size:
                    raise SourceAcquisitionError(
                        f"Incomplete download from {url}: received {observed_size} of "
                        f"{expected_size} bytes"
                    )
                partial_path.replace(destination)
                return DownloadedResource(destination, metadata)
            except requests.HTTPError as error:
                last_error = error
                status = error.response.status_code if error.response is not None else None
                if status not in {408, 429} and (status is None or status < 500):
                    partial_path.unlink(missing_ok=True)
                    raise SourceAcquisitionError(
                        f"Could not download {url}: {error}"
                    ) from error
            except (OSError, requests.RequestException, SourceAcquisitionError) as error:
                last_error = error

            if attempt < self._resumable_attempts:
                sleep(self._resumable_retry_delay_seconds * 2 ** (attempt - 1))

        partial_path.unlink(missing_ok=True)
        raise SourceAcquisitionError(
            f"Could not download {url} after {self._resumable_attempts} attempts: "
            f"{last_error}"
        ) from last_error

    def get_json(
        self,
        url: str,
        *,
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> HttpJson:
        try:
            with self._session.get(
                url,
                timeout=timeout,
                headers={"Accept": "application/json", **dict(headers or {})},
            ) as response:
                response.raise_for_status()
                return HttpJson(response.json(), self._metadata(response))
        except (ValueError, requests.RequestException) as error:
            raise SourceAcquisitionError(f"Could not read JSON from {url}: {error}") from error

    def post_json(self, url: str, payload: Mapping[str, Any], *, timeout: float) -> HttpJson:
        try:
            with self._session.post(
                url,
                json=dict(payload),
                timeout=timeout,
                headers={"Accept": "application/json"},
            ) as response:
                response.raise_for_status()
                return HttpJson(response.json(), self._metadata(response))
        except (ValueError, requests.RequestException) as error:
            raise SourceAcquisitionError(f"Could not read JSON from {url}: {error}") from error

    def post_download(
        self,
        url: str,
        payload: Mapping[str, Any],
        destination: Path,
        *,
        timeout: float,
        accept: str | None = None,
    ) -> DownloadedResource:
        destination = Path(destination)
        partial_path = destination.with_name(f"{destination.name}.part")
        destination.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Accept": accept} if accept else None
        try:
            with self._session.post(
                url,
                json=dict(payload),
                timeout=timeout,
                headers=headers,
                stream=True,
            ) as response:
                return self._stream_to_file(response, destination, partial_path, url)
        except SourceAcquisitionError:
            partial_path.unlink(missing_ok=True)
            raise
        except (OSError, requests.RequestException) as error:
            partial_path.unlink(missing_ok=True)
            raise SourceAcquisitionError(f"Could not download {url}: {error}") from error

    @staticmethod
    def _metadata(response: requests.Response) -> HttpMetadata:
        return HttpMetadata(
            final_url=str(response.url),
            headers=dict(response.headers),
        )

    def _stream_to_file(
        self,
        response: requests.Response,
        destination: Path,
        partial_path: Path,
        url: str,
    ) -> DownloadedResource:
        response.raise_for_status()
        metadata = self._metadata(response)
        size = 0
        with partial_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=self._chunk_size):
                if chunk:
                    handle.write(chunk)
                    size += len(chunk)
        if size == 0:
            raise SourceAcquisitionError(f"Downloaded empty response from {url}")
        partial_path.replace(destination)
        return DownloadedResource(destination, metadata)


def _resume_validator(metadata: HttpMetadata) -> str | None:
    etag = metadata.header("ETag")
    if etag and not etag.strip().startswith("W/"):
        return etag
    return metadata.header("Last-Modified")


def _validate_content_range(
    value: str | None,
    *,
    offset: int,
    expected_size: int,
) -> None:
    if not value:
        raise SourceAcquisitionError("Resumed response omitted Content-Range")
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", value.strip())
    if (
        match is None
        or int(match.group(1)) != offset
        or int(match.group(3)) != expected_size
        or int(match.group(2)) < offset
    ):
        raise SourceAcquisitionError(
            f"Resumed response has invalid Content-Range {value!r}"
        )
