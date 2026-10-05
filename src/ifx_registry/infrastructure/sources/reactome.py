"""Reactome pathway source declaration and validation."""

from __future__ import annotations

import re
from datetime import date

from ifx_registry.application.contracts import VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources._dates import parse_http_date
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
    require_download,
)
from ifx_registry.infrastructure.sources.version_strategies import (
    SourceVersionStrategy,
    TextEndpointVersionStrategy,
)

REACTOME_VERSION_URL = "https://reactome.org/ContentService/data/database/version"
REACTOME_HOMEPAGE = "https://reactome.org/"
REACTOME_INTERACTOR_NAME = "reactome.homo_sapiens.interactions.tab-delimited.txt"

REACTOME_FILES = (
    HttpFileSpec(
        "https://reactome.org/download/current/ReactomePathways.txt",
        "ReactomePathways.txt",
    ),
    HttpFileSpec(
        "https://reactome.org/download/current/NCBI2Reactome_All_Levels.txt",
        "NCBI2Reactome_All_Levels.txt",
    ),
    HttpFileSpec(
        "https://reactome.org/download/current/ReactomePathways.gmt.zip",
        "ReactomePathways.gmt.zip",
    ),
    HttpFileSpec(
        "https://reactome.org/download/current/ReactomePathwaysRelation.txt",
        "ReactomePathwaysRelation.txt",
    ),
    HttpFileSpec(
        "https://reactome.org/download/current/UniProt2Reactome_All_Levels.txt",
        "UniProt2Reactome_All_Levels.txt",
    ),
    HttpFileSpec(
        "https://reactome.org/download/current/ChEBI2Reactome_All_Levels.txt",
        "ChEBI2Reactome_All_Levels.txt",
    ),
    HttpFileSpec(
        "https://reactome.org/download/current/interactors/"
        "reactome.homo_sapiens.interactions.tab-delimited.txt",
        REACTOME_INTERACTOR_NAME,
    ),
)

REACTOME_VERSION_STRATEGY = TextEndpointVersionStrategy(
    url=REACTOME_VERSION_URL,
    evidence_method="reactome_database_version",
    blank_response_message="Reactome database version response was blank",
    description=(
        "Reads the Reactome database release number from the Reactome Content "
        "Service before and after downloading the files."
    ),
)


class ReactomeBundleVersionStrategy(SourceVersionStrategy):
    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return REACTOME_VERSION_STRATEGY.evidence_urls

    @property
    def description(self) -> str:
        return REACTOME_VERSION_STRATEGY.description + " Uses bundle1 for the seven-file contract."

    def discover(self, http: HttpGateway, request: VersionProbeRequest) -> SourceVersion:
        release = REACTOME_VERSION_STRATEGY.discover(http, request)
        if not release.value.isdigit():
            raise SourceValidationError("Reactome release must be an integer")
        return SourceVersion(
            f"{release.value}-bundle1",
            evidence={**release.evidence, "upstream_release": release.value, "bundle_revision": 1},
        )


class ReactomePathwaysSource(HttpSnapshotSource):
    """Reactome's complete pathway input set."""

    _dataset = DatasetId(source="reactome", dataset="pathways")

    def __init__(self, http: HttpGateway):
        super().__init__(http)

    @property
    def dataset(self) -> DatasetId:
        return self._dataset

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return REACTOME_FILES

    @property
    def homepage(self) -> str:
        return REACTOME_HOMEPAGE

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return ReactomeBundleVersionStrategy()

    @property
    def version_check_message(self) -> str:
        return "Checking the Reactome database release"

    @property
    def validation_message(self) -> str:
        return "Validating the Reactome release metadata"

    @property
    def confirmation_message(self) -> str:
        return "Confirming the Reactome release did not change during download"

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        counts = {}
        for name, columns, id_column in (
            ("ReactomePathways.txt", 3, 0),
            ("NCBI2Reactome_All_Levels.txt", 6, 1),
        ):
            rows = human_rows = non_numeric_ids = 0
            with require_download(downloads, name).resource.path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    fields = line.rstrip("\r\n").split("\t")
                    if (
                        len(fields) != columns
                        or not re.fullmatch(r"R-[A-Z]{3}-\d+", fields[id_column])
                        or not fields[-1].strip()
                        or (id_column == 1 and not fields[0].strip())
                    ):
                        raise SourceValidationError(f"Invalid {name} record at row {rows + 1}")
                    non_numeric_ids += id_column == 1 and not fields[0].isdigit()
                    rows += 1
                    human_rows += fields[-1] == "Homo sapiens"
            if not human_rows:
                raise SourceValidationError(f"{name} contains no human records")
            counts[name] = {
                "rows": rows,
                "human_rows": human_rows,
                "non_numeric_source_ids": non_numeric_ids,
            }
        interactor = require_download(downloads, REACTOME_INTERACTOR_NAME)
        last_modified = interactor.resource.metadata.header("last-modified")
        if not last_modified:
            raise SourceValidationError(
                "Reactome interactor response did not include a Last-Modified header"
            )
        version_date: date = parse_http_date(
            last_modified,
            field_name="Reactome interactor Last-Modified",
        )
        enriched_version = SourceVersion(
            value=version.value,
            version_date=version_date,
            discovered_at=version.discovered_at,
            evidence={
                **version.evidence,
                "interactor_last_modified": last_modified,
            },
        )
        return SourceValidationResult(
            version=enriched_version,
            metadata={"version_method": "reactome_database_version", "added_file_counts": counts},
        )
