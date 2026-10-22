"""NCBI MedGen mapping files, published under their decompressed filenames."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime

from ifx_registry.application.contracts import VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
)
from ifx_registry.infrastructure.sources.version_strategies import SourceVersionStrategy


MEDGEN_FILES = tuple(
    HttpFileSpec(
        f"https://ftp.ncbi.nlm.nih.gov/pub/medgen/{name}.gz",
        name,
        gzip_decompress=True,
    )
    for name in (
        "MedGenIDMappings.txt",
        "MGSTY.RRF",
        "MedGen_HPO_Mapping.txt",
        "MedGen_HPO_OMIM_Mapping.txt",
    )
)


@dataclass(frozen=True, slots=True)
class MedGenLastModifiedStrategy(SourceVersionStrategy):
    files: tuple[HttpFileSpec, ...] = MEDGEN_FILES
    description: str = (
        "Uses the newest NCBI MedGen Last-Modified date and records every file timestamp."
    )

    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return tuple(file.url for file in self.files)

    def discover(self, http: HttpGateway, request: VersionProbeRequest) -> SourceVersion:
        observations: list[dict[str, str]] = []
        dates: list[date] = []
        for file in self.files:
            metadata = http.head(file.url, timeout=request.timeout.total_seconds())
            raw = metadata.header("Last-Modified")
            if not raw:
                raise SourceValidationError(
                    f"NCBI MedGen file {file.name} has no Last-Modified header"
                )
            try:
                observed_date = parsedate_to_datetime(raw).date()
            except (TypeError, ValueError) as error:
                raise SourceValidationError(
                    f"Could not parse MedGen Last-Modified header for {file.name}: {raw!r}"
                ) from error
            dates.append(observed_date)
            observations.append(
                {"file": file.name, "url": metadata.final_url,
                 "last_modified": raw, "date": observed_date.isoformat()}
            )
        version_date = max(dates)
        return SourceVersion(
            version_date.isoformat(),
            version_date=version_date,
            evidence={"method": "multi_file_max_last_modified", "files": observations},
        )


class NcbiMedGenSource(HttpSnapshotSource):
    """Acquire the four public MedGen mapping products as a single dated set."""

    def __init__(self, http: HttpGateway):
        super().__init__(http)
        self._version_strategy = MedGenLastModifiedStrategy()

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("ncbi", "medgen")

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return MEDGEN_FILES

    @property
    def homepage(self) -> str:
        return "https://www.ncbi.nlm.nih.gov/medgen/"

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return self._version_strategy

    def validate_downloads(
        self, version: SourceVersion, downloads: tuple[DownloadedSourceFile, ...]
    ) -> SourceValidationResult:
        observations = {item["file"]: item for item in version.evidence["files"]}
        for download in downloads:
            name = download.spec.name
            expected = observations[name]["last_modified"]
            actual = download.resource.metadata.header("Last-Modified")
            if actual != expected:
                raise SourceValidationError(
                    f"NCBI MedGen file {name} changed during download: "
                    f"expected {expected!r}, received {actual!r}"
                )
            if download.resource.path.stat().st_size == 0:
                raise SourceValidationError(f"NCBI MedGen file {name} is empty after decompression")
        return SourceValidationResult(version)

    def validate_confirmation(
        self, version: SourceVersion, confirmed_version: SourceVersion
    ) -> None:
        super().validate_confirmation(version, confirmed_version)
        if version.evidence["files"] != confirmed_version.evidence["files"]:
            raise SourceValidationError("NCBI MedGen files changed during acquisition")
