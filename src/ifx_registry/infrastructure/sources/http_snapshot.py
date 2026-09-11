"""Reusable acquisition workflow for versioned HTTP file sets."""

from __future__ import annotations

import gzip
import shutil
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.application.progress import ProgressUpdate
from ifx_registry.application.versioning import ensure_expected_version
from ifx_registry.domain.errors import SourceContractError
from ifx_registry.domain.models import DatasetId, SnapshotFile, SourceSnapshot, SourceVersion
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway
from ifx_registry.infrastructure.sources.version_strategies import SourceVersionStrategy
from ifx_registry.infrastructure.workspace import SnapshotWorkspace


@dataclass(frozen=True, slots=True)
class HttpFileSpec:
    """One upstream file and its stable name inside a Registry snapshot."""

    url: str
    name: str
    gzip_download: bool = False

    def __post_init__(self) -> None:
        path = PurePosixPath(self.name)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("HTTP snapshot file name must be a safe relative POSIX path")
        if not self.url.strip():
            raise ValueError("HTTP snapshot file URL must not be blank")


@dataclass(frozen=True, slots=True)
class DownloadedSourceFile:
    """A downloaded file paired with the declaration that requested it."""

    spec: HttpFileSpec
    resource: DownloadedResource


@dataclass(frozen=True, slots=True)
class SourceValidationResult:
    """Source-specific validation output retained in the snapshot manifest."""

    version: SourceVersion
    metadata: Mapping[str, Any] = field(default_factory=dict)


class HttpSnapshotSource(SourceAdapter):
    """Template method for safely acquiring a versioned set of HTTP files."""

    def __init__(self, http: HttpGateway):
        self._http = http

    @property
    @abstractmethod
    def dataset(self) -> DatasetId:
        """Return the stable Registry identity of this source dataset."""

    @property
    @abstractmethod
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        """Return the complete ordered file set for one snapshot."""

    @property
    @abstractmethod
    def homepage(self) -> str | None:
        """Return the source homepage shown to users."""

    @property
    @abstractmethod
    def version_strategy(self) -> SourceVersionStrategy:
        """Return the reusable strategy used to discover a release."""

    @property
    def expected_file_count(self) -> int:
        return len(self.file_specs)

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return tuple(file.url for file in self.file_specs)

    @property
    def version_check_description(self) -> str:
        return self.version_strategy.description

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return self.version_strategy.evidence_urls

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        return self.version_strategy.discover(self._http, request)

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        request.progress.report(
            ProgressUpdate(stage="checking", message=self.version_check_message)
        )
        version = self.discover_latest(VersionProbeRequest(timeout=request.timeout))
        ensure_expected_version(self.dataset, version, request.expected_version)
        file_specs = self.file_specs_for(version)
        self._validate_declaration(file_specs)
        snapshot_dir = (
            request.destination / self.dataset.source / self.dataset.dataset / version.value
        )

        with SnapshotWorkspace(snapshot_dir) as workspace:
            downloads = self._download_files(request, workspace, file_specs)
            request.progress.report(
                ProgressUpdate(stage="validating", message=self.validation_message)
            )
            validation = self.validate_downloads(version, downloads)
            if validation.version.value != version.value:
                raise SourceContractError(
                    f"Validation for {self.dataset} changed version {version.value!r} "
                    f"to {validation.version.value!r}"
                )
            request.progress.report(
                ProgressUpdate(stage="confirming", message=self.confirmation_message)
            )
            confirmed_version = self.discover_latest(VersionProbeRequest(timeout=request.timeout))
            ensure_expected_version(self.dataset, confirmed_version, version)
            workspace.commit()

        return SourceSnapshot(
            dataset=self.dataset,
            version=validation.version,
            files=tuple(
                SnapshotFile(
                    local_path=snapshot_dir / downloaded.spec.name,
                    relative_path=PurePosixPath(downloaded.spec.name),
                    source_url=downloaded.resource.metadata.final_url,
                    content_type=downloaded.resource.metadata.header("content-type"),
                )
                for downloaded in downloads
            ),
            downloaded_at=datetime.now(UTC),
            homepage=self.homepage,
            upstream_urls=_unique_urls(
                tuple(file.url for file in file_specs) + self.version_evidence_urls
            ),
            metadata=validation.metadata,
        )

    @property
    def version_check_message(self) -> str:
        return f"Checking the latest {self.dataset} release"

    @property
    def validation_message(self) -> str:
        return f"Validating the downloaded {self.dataset} files"

    @property
    def confirmation_message(self) -> str:
        return f"Confirming the {self.dataset} release did not change during download"

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        """Validate staged files and optionally enrich their version metadata."""
        return SourceValidationResult(version=version)

    def file_specs_for(self, version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        """Resolve files for a version; moving-release sources may override this."""

        del version
        return self.file_specs

    def _download_files(
        self,
        request: FetchRequest,
        workspace: SnapshotWorkspace,
        file_specs: tuple[HttpFileSpec, ...],
    ) -> tuple[DownloadedSourceFile, ...]:
        downloads: list[DownloadedSourceFile] = []
        for index, file_spec in enumerate(file_specs, start=1):
            request.progress.report(
                ProgressUpdate(
                    stage="downloading",
                    message=f"Downloading {file_spec.name}",
                    completed=index,
                    total=len(file_specs),
                )
            )
            resource = self._http.download(
                file_spec.url,
                (
                    workspace.staging_path / f".{file_spec.name}.uncompressed"
                    if file_spec.gzip_download
                    else workspace.staging_path / file_spec.name
                ),
                timeout=request.timeout.total_seconds(),
            )
            if file_spec.gzip_download:
                compressed_path = workspace.staging_path / file_spec.name
                with resource.path.open("rb") as source_handle:
                    with gzip.open(compressed_path, "wb") as compressed_handle:
                        shutil.copyfileobj(source_handle, compressed_handle)
                resource.path.unlink()
                resource = DownloadedResource(compressed_path, resource.metadata)
            downloads.append(DownloadedSourceFile(file_spec, resource))
        return tuple(downloads)

    def _validate_declaration(self, file_specs: tuple[HttpFileSpec, ...]) -> None:
        if not file_specs:
            raise SourceContractError(f"HTTP source {self.dataset} declares no files")
        names = [file.name for file in file_specs]
        if len(names) != len(set(names)):
            raise SourceContractError(
                f"HTTP source {self.dataset} declares duplicate snapshot file names"
            )


def require_download(
    downloads: tuple[DownloadedSourceFile, ...],
    name: str,
) -> DownloadedSourceFile:
    """Return one required file from a completed snapshot download."""
    try:
        return next(download for download in downloads if download.spec.name == name)
    except StopIteration as error:
        raise SourceContractError(f"Required downloaded file {name!r} was not present") from error


def _unique_urls(urls: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(urls))
