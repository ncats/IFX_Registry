"""Monarch KG human gene-disease associations from a pinned KG release."""

from __future__ import annotations

import csv
import gzip
import re
from datetime import date

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

MONARCH_BASE = "https://data.monarchinitiative.org/monarch-kg"
MONARCH_FILE = "monarch_gene_disease.9606.tsv.gz"
MONARCH_UPSTREAM_FILE = "gene_disease.9606.tsv.gz"
MONARCH_COLUMNS = (
    "subject", "subject_label", "subject_category", "subject_taxon",
    "subject_taxon_label", "negated", "predicate", "object", "object_label",
    "object_category", "qualifiers", "publications", "has_evidence",
    "primary_knowledge_source", "aggregator_knowledge_source",
)


def parse_monarch_listing(text: str) -> SourceVersion:
    releases = set()
    for value in re.findall(
        rf'href="{re.escape(MONARCH_BASE)}/(\d{{4}}-\d{{2}}-\d{{2}})/index\.html"',
        text,
    ):
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise SourceValidationError(f"Invalid Monarch KG release date {value}") from error
        releases.add(value)
    if not releases:
        raise SourceValidationError("Monarch KG index contains no dated release directories")
    latest = max(releases)
    return SourceVersion(
        latest, version_date=date.fromisoformat(latest), evidence={"release": latest}
    )


def monarch_file_url(release: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release):
        raise SourceValidationError(f"Invalid Monarch KG release {release!r}")
    return f"{MONARCH_BASE}/{release}/tsv/gene_associations/{MONARCH_UPSTREAM_FILE}"


class MonarchGeneDiseaseSource(HttpSnapshotSource):
    def __init__(self, http: HttpGateway):
        super().__init__(http)
        self._strategy = ParsedTextVersionStrategy(
            f"{MONARCH_BASE}/",
            parse_monarch_listing,
            "monarch_kg_release_index",
            "Reads Monarch KG's dated release index and fetches the pinned release file.",
        )

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("monarch", "gene_disease_associations")

    @property
    def homepage(self) -> str:
        return "https://monarchinitiative.org/"

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return ()

    @property
    def expected_file_count(self) -> int:
        return 1

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (f"{MONARCH_BASE}/",)

    def file_specs_for(self, version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        return (HttpFileSpec(monarch_file_url(version.value), MONARCH_FILE),)

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return self._strategy

    def validate_downloads(
        self, version: SourceVersion, downloads: tuple[DownloadedSourceFile, ...]
    ) -> SourceValidationResult:
        path = require_download(downloads, MONARCH_FILE).resource.path
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle, delimiter="\t")
                if tuple(next(reader, ())) != MONARCH_COLUMNS:
                    raise SourceValidationError("Monarch gene-disease TSV header changed")
                rows = 0
                for row in reader:
                    if len(row) != len(MONARCH_COLUMNS):
                        raise SourceValidationError(
                            f"Malformed Monarch gene-disease row {rows + 1}"
                        )
                    rows += 1
        except (OSError, EOFError, UnicodeError, csv.Error) as error:
            raise SourceValidationError(
                f"Could not read Monarch gene-disease gzip: {error}"
            ) from error
        if rows == 0:
            raise SourceValidationError("Monarch gene-disease TSV has no data rows")
        return SourceValidationResult(
            version, metadata={"rows": rows, "monarch_kg_release": version.value}
        )
