"""ChEBI full ontology acquisition and release consistency validation."""

from __future__ import annotations

import gzip
import re
from datetime import date, datetime

from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
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

CHEBI_README_URL = "https://ftp.ebi.ac.uk/pub/databases/chebi/ontology/README"
CHEBI_OBO_URL = "https://ftp.ebi.ac.uk/pub/databases/chebi/ontology/chebi.obo.gz"
CHEBI_FILES = (HttpFileSpec(CHEBI_OBO_URL, "chebi.obo.gz"),)


def parse_chebi_release(text: str) -> SourceVersion:
    release = re.search(r"ChEBI Release:\s*(\S+)", text)
    updated = re.search(r"Date of last update:\s*(\d{4}-\d{2}-\d{2})", text)
    if release is None or updated is None:
        raise SourceValidationError("Could not parse the ChEBI release README")
    return SourceVersion(
        release.group(1),
        version_date=date.fromisoformat(updated.group(1)),
        evidence={"readme_update_date": updated.group(1)},
    )


class ChebiFullOntologySource(HttpSnapshotSource):
    def __init__(self, http: HttpGateway):
        super().__init__(http)
        self._strategy = ParsedTextVersionStrategy(
            CHEBI_README_URL,
            parse_chebi_release,
            "chebi_release_readme",
            "Reads the release number and date from ChEBI's ontology README, then "
            "confirms that the downloaded ontology declares the same release.",
        )

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("chebi", "ontology_full")

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return CHEBI_FILES

    @property
    def homepage(self) -> str:
        return "https://www.ebi.ac.uk/chebi/"

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return self._strategy

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        ontology = require_download(downloads, "chebi.obo.gz")
        data_version = None
        raw_date = None
        with gzip.open(ontology.resource.path, "rt", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index > 300:
                    break
                if line.startswith("data-version:"):
                    data_version = line.split(":", 1)[1].strip()
                elif line.startswith("date:"):
                    raw_date = line.split(":", 1)[1].strip()
                if data_version and raw_date:
                    break
        if data_version != version.value:
            raise SourceValidationError(
                f"ChEBI README release {version.value} does not match ontology "
                f"data-version {data_version!r}"
            )
        ontology_date = _parse_obo_date(raw_date)
        return SourceValidationResult(
            SourceVersion(
                version.value,
                version_date=version.version_date,
                discovered_at=version.discovered_at,
                evidence={
                    **version.evidence,
                    "obo_data_version": data_version,
                    "obo_date": ontology_date.isoformat() if ontology_date else raw_date,
                },
            ),
            {"validation": {"readme_matches_ontology": True}},
        )


def _parse_obo_date(value: str | None) -> date | None:
    if not value:
        return None
    match = re.match(r"(\d{2}):(\d{2}):(\d{4})", value)
    if match is None:
        return None
    day, month, year = match.groups()
    return datetime.strptime(f"{year}-{month}-{day}", "%Y-%m-%d").date()
