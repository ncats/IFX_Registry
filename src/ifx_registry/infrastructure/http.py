"""Shared HTTP boundary used by source adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
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

    def get_json(self, url: str, *, timeout: float) -> HttpJson:
        """Read JSON when supported by the concrete HTTP adapter."""
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
    ):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if not user_agent.strip():
            raise ValueError("user_agent must not be blank")
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._chunk_size = chunk_size

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

    def get_json(self, url: str, *, timeout: float) -> HttpJson:
        try:
            with self._session.get(
                url,
                timeout=timeout,
                headers={"Accept": "application/json"},
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
