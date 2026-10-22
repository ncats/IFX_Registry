"""Orphadata's versioned rare disease ontology."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

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

ORDO_PAGE = "https://sciences.orphadata.com/ordo/"
ORDO_BASE = "https://www.orphadata.com/data/ontologies/ordo/last_version"


def ordo_file_name(version: str) -> str:
    if not re.fullmatch(r"\d+\.\d+", version):
        raise SourceValidationError(f"Invalid ORDO version {version!r}")
    return f"ORDO_en_{version}.owl"


def ordo_file_url(version: str) -> str:
    return f"{ORDO_BASE}/{ordo_file_name(version)}"


def parse_ordo_listing(text: str) -> SourceVersion:
    versions = {
        tuple(int(part) for part in value.split(".")): value
        for value in re.findall(
            rf'href="{re.escape(ORDO_BASE)}/ORDO_en_(\d+\.\d+)\.owl"', text
        )
    }
    if not versions:
        raise SourceValidationError("Orphadata ORDO page has no English OWL release")
    latest = versions[max(versions)]
    return SourceVersion(latest, evidence={"ordo_release": latest})


def validate_ordo_file(path, expected_version: str) -> int:
    found_versions: list[str] = []
    classes = 0
    try:
        for _, element in ET.iterparse(path, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "versionInfo":
                found_versions.append((element.text or "").strip())
            elif tag == "Class":
                classes += 1
            element.clear()
    except (OSError, ET.ParseError) as error:
        raise SourceValidationError(f"Could not parse Orphadata ORDO OWL: {error}") from error
    if found_versions != [expected_version]:
        raise SourceValidationError(
            f"Orphadata ORDO OWL versionInfo {found_versions!r} "
            f"does not match release {expected_version}"
        )
    if classes == 0:
        raise SourceValidationError("Orphadata ORDO OWL contains no classes")
    return classes


class OrphanetOrdoSource(HttpSnapshotSource):
    def __init__(self, http: HttpGateway):
        super().__init__(http)
        self._strategy = ParsedTextVersionStrategy(
            ORDO_PAGE,
            parse_ordo_listing,
            "orphanet_ordo_release_page",
            "Reads the English ORDO release version from Orphadata's release page.",
        )

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("orphanet", "ordo")

    @property
    def homepage(self) -> str:
        return ORDO_PAGE

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return ()

    @property
    def expected_file_count(self) -> int:
        return 1

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (ORDO_PAGE,)

    def file_specs_for(self, version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        name = ordo_file_name(version.value)
        return (HttpFileSpec(ordo_file_url(version.value), name),)

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return self._strategy

    def validate_downloads(
        self, version: SourceVersion, downloads: tuple[DownloadedSourceFile, ...]
    ) -> SourceValidationResult:
        file = require_download(downloads, ordo_file_name(version.value))
        classes = validate_ordo_file(file.resource.path, version.value)
        return SourceValidationResult(version, metadata={"ordo_classes": classes})
