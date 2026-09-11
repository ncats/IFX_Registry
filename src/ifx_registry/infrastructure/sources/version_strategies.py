"""Reusable strategies for discovering upstream source versions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from ifx_registry.application.contracts import VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import HttpGateway


class SourceVersionStrategy(ABC):
    """Discover a source version using one reusable upstream protocol."""

    @property
    @abstractmethod
    def evidence_urls(self) -> tuple[str, ...]:
        """Return the upstream URLs used as version evidence."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Explain the version check in plain language for Registry users."""

    @abstractmethod
    def discover(
        self,
        http: HttpGateway,
        request: VersionProbeRequest,
    ) -> SourceVersion:
        """Discover the latest upstream version."""


@dataclass(frozen=True, slots=True)
class TextEndpointVersionStrategy(SourceVersionStrategy):
    """Read a version identifier from the body of a small text endpoint."""

    url: str
    evidence_method: str
    blank_response_message: str
    description: str

    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return (self.url,)

    def discover(
        self,
        http: HttpGateway,
        request: VersionProbeRequest,
    ) -> SourceVersion:
        response = http.get_text(self.url, timeout=request.timeout.total_seconds())
        version = response.text.strip()
        if not version:
            raise SourceValidationError(self.blank_response_message)
        return SourceVersion(
            value=version,
            evidence={
                "method": self.evidence_method,
                "version_url": response.metadata.final_url,
            },
        )


@dataclass(frozen=True, slots=True)
class HeaderVersionStrategy(SourceVersionStrategy):
    """Read a version and optional release date from HTTP response headers."""

    url: str
    version_header: str
    evidence_method: str
    missing_version_message: str
    description: str
    date_header: str | None = None
    date_parser: Callable[[str], date] | None = None

    def __post_init__(self) -> None:
        if (self.date_header is None) != (self.date_parser is None):
            raise ValueError("date_header and date_parser must be supplied together")

    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return (self.url,)

    def discover(
        self,
        http: HttpGateway,
        request: VersionProbeRequest,
    ) -> SourceVersion:
        metadata = http.head(self.url, timeout=request.timeout.total_seconds())
        version = metadata.header(self.version_header)
        if not version:
            raise SourceValidationError(self.missing_version_message)

        raw_date = metadata.header(self.date_header) if self.date_header else None
        version_date = self.date_parser(raw_date) if raw_date and self.date_parser else None
        evidence: dict[str, str | None] = {
            "method": self.evidence_method,
            "probe_url": metadata.final_url,
            _evidence_key(self.version_header): version,
        }
        if self.date_header:
            evidence[_evidence_key(self.date_header)] = raw_date
        return SourceVersion(
            value=version,
            version_date=version_date,
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class ParsedTextVersionStrategy(SourceVersionStrategy):
    """Parse a source-specific release from a small text or HTML endpoint."""

    url: str
    parser: Callable[[str], SourceVersion]
    evidence_method: str
    description: str

    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return (self.url,)

    def discover(
        self,
        http: HttpGateway,
        request: VersionProbeRequest,
    ) -> SourceVersion:
        response = http.get_text(self.url, timeout=request.timeout.total_seconds())
        parsed = self.parser(response.text)
        return SourceVersion(
            value=parsed.value,
            version_date=parsed.version_date,
            discovered_at=parsed.discovered_at,
            evidence={
                **parsed.evidence,
                "method": self.evidence_method,
                "version_url": response.metadata.final_url,
            },
        )


@dataclass(frozen=True, slots=True)
class FixedVersionStrategy(SourceVersionStrategy):
    """Declare a reviewed release that changes only through code review."""

    version: SourceVersion
    description: str
    evidence_urls: tuple[str, ...]

    def discover(
        self,
        http: HttpGateway,
        request: VersionProbeRequest,
    ) -> SourceVersion:
        del http, request
        return self.version


def _evidence_key(header: str) -> str:
    return header.casefold().replace("-", "_")
