"""Ports for source-specific version discovery and fetching."""

from abc import ABC, abstractmethod

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.domain.models import DatasetId, SourceSnapshot, SourceVersion


class SourceFetcher(ABC):
    """Fetch one logical source dataset without publishing or transforming it."""

    @property
    @abstractmethod
    def dataset(self) -> DatasetId:
        """Return the stable source and dataset identity implemented by this adapter."""

    @abstractmethod
    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        """Fetch files into the requested destination and describe the snapshot."""


class SourceVersionProbe(ABC):
    """Discover the latest upstream version of one logical source dataset."""

    @property
    @abstractmethod
    def dataset(self) -> DatasetId:
        """Return the stable source and dataset identity implemented by this adapter."""

    @abstractmethod
    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        """Return the latest discoverable upstream version and supporting evidence."""


class SourceAdapter(SourceFetcher, SourceVersionProbe, ABC):
    """Combined port for an automatically discoverable and fetchable source."""

    @property
    @abstractmethod
    def homepage(self) -> str | None:
        """Return the upstream publisher page shown to Registry users."""

    @property
    @abstractmethod
    def upstream_urls(self) -> tuple[str, ...]:
        """Return the upstream endpoints used to acquire this dataset."""

    @property
    @abstractmethod
    def version_check_description(self) -> str:
        """Explain in plain language how the upstream version is discovered."""

    @property
    @abstractmethod
    def version_evidence_urls(self) -> tuple[str, ...]:
        """Return the endpoints consulted to discover the upstream version."""
