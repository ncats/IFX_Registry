"""Preserve Babel's full Disease.txt compendium for disease harmonization."""

from __future__ import annotations

import json

from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.babel import (
    BABEL_ROOT_URL,
    parse_babel_release_listing,
)
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
    require_download,
)
from ifx_registry.infrastructure.sources.version_strategies import (
    ParsedTextVersionStrategy,
    SourceVersionStrategy,
)

DISEASE_MEMBER = "nodenorm_disease.txt"


class BabelDiseaseCompendiumSource(HttpSnapshotSource):
    def __init__(self, http: HttpGateway, *, minimum_records: int = 300_000):
        super().__init__(http)
        if minimum_records < 1:
            raise ValueError("minimum_records must be positive")
        self._minimum_records = minimum_records
        self._strategy = ParsedTextVersionStrategy(
            BABEL_ROOT_URL,
            parse_babel_release_listing,
            "newest_versioned_babel_directory",
            "Selects the newest dated Babel release and downloads its full Disease.txt.",
        )

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("babel", "disease_compendium")

    @property
    def homepage(self) -> str:
        return "https://github.com/TranslatorSRI/Babel"

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return ()

    @property
    def expected_file_count(self) -> int:
        return 1

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (BABEL_ROOT_URL,)

    def file_specs_for(self, version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        parse_babel_release_listing(f'<a href="{version.value}/">release</a>')
        return (
            HttpFileSpec(
                f"{BABEL_ROOT_URL}{version.value}/compendia/Disease.txt",
                DISEASE_MEMBER,
            ),
        )

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return self._strategy

    def validate_downloads(
        self, version: SourceVersion, downloads: tuple[DownloadedSourceFile, ...]
    ) -> SourceValidationResult:
        downloaded = require_download(downloads, DISEASE_MEMBER)
        path = downloaded.resource.path
        declared_size = downloaded.resource.metadata.header("Content-Length")
        if declared_size and declared_size.isdigit() and path.stat().st_size != int(declared_size):
            raise SourceValidationError("Babel Disease.txt size differs from Content-Length")
        records = 0
        try:
            with path.open("rb") as handle:
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        raise SourceValidationError(
                            f"Babel Disease.txt record {records + 1} has no newline"
                        )
                    value = json.loads(raw)
                    if (
                        not isinstance(value, dict)
                        or value.get("type") != "biolink:Disease"
                        or not isinstance(value.get("identifiers"), list)
                        or not value["identifiers"]
                        or not isinstance(value.get("taxa"), list)
                    ):
                        raise SourceValidationError(
                            f"Babel Disease.txt record {records + 1} is malformed"
                        )
                    records += 1
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SourceValidationError(f"Could not read Babel Disease.txt: {error}") from error
        if records < self._minimum_records:
            raise SourceValidationError(
                f"Babel Disease.txt has {records} records; "
                f"minimum is {self._minimum_records}"
            )
        return SourceValidationResult(version, metadata={"disease_records": records})
