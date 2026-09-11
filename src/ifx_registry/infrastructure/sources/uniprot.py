"""UniProt human proteome source declaration and validation."""

from __future__ import annotations

import gzip
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import ijson

from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources._dates import parse_flexible_date
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
    require_download,
)
from ifx_registry.infrastructure.sources.version_strategies import (
    HeaderVersionStrategy,
    SourceVersionStrategy,
)

UNIPROT_RELEASE_PROBE_URL = (
    "https://rest.uniprot.org/uniprotkb/stream?"
    "compressed=false&format=json&size=1&query=accession:P04637"
)
UNIPROT_HUMAN_URL = (
    "https://rest.uniprot.org/uniprotkb/stream?"
    "compressed=true&format=json&query=(*)+AND+(model_organism:9606)"
)
UNIPROT_REVIEWED_HUMAN_URL = (
    "https://rest.uniprot.org/uniprotkb/stream?"
    "compressed=true&format=json&query=(reviewed:true)+AND+(model_organism:9606)"
)
UNIPROT_HOMEPAGE = "https://www.uniprot.org/"
UNIPROT_HUMAN_NAME = "uniprot-human.json.gz"
UNIPROT_REVIEWED_HUMAN_NAME = "uniprot-human-reviewed.json.gz"

UNIPROT_FILES = (
    HttpFileSpec(UNIPROT_HUMAN_URL, UNIPROT_HUMAN_NAME),
    HttpFileSpec(UNIPROT_REVIEWED_HUMAN_URL, UNIPROT_REVIEWED_HUMAN_NAME),
)


def normalize_uniprot_release_date(value: str | None) -> date | None:
    """Normalize the optional UniProt release-date header."""
    if not value:
        return None
    return parse_flexible_date(value, field_name="UniProt release")


def _parse_required_uniprot_release_date(value: str) -> date:
    parsed = normalize_uniprot_release_date(value)
    if parsed is None:
        raise SourceValidationError("UniProt release date was blank")
    return parsed


UNIPROT_VERSION_STRATEGY = HeaderVersionStrategy(
    url=UNIPROT_RELEASE_PROBE_URL,
    version_header="X-UniProt-Release",
    evidence_method="uniprot_release_headers",
    missing_version_message="UniProt response did not include X-UniProt-Release",
    description=(
        "Reads the X-UniProt-Release and X-UniProt-Release-Date response headers "
        "before and after downloading the files."
    ),
    date_header="X-UniProt-Release-Date",
    date_parser=_parse_required_uniprot_release_date,
)


@dataclass(frozen=True, slots=True)
class AccessionScan:
    records: int
    primary: frozenset[str]
    secondary: frozenset[str]
    reviewed_primary: frozenset[str]


def _scan_accessions(path: Path) -> AccessionScan:
    primary: set[str] = set()
    secondary: set[str] = set()
    reviewed_primary: set[str] = set()
    records = 0

    try:
        with gzip.open(path, "rb") as handle:
            for item in ijson.items(handle, "results.item"):
                if not isinstance(item, Mapping):
                    raise SourceValidationError(f"UniProt record in {path.name} was not an object")
                records += 1
                primary_accession = item.get("primaryAccession")
                if isinstance(primary_accession, str) and primary_accession:
                    primary.add(primary_accession)
                secondary_accessions = item.get("secondaryAccessions") or []
                if isinstance(secondary_accessions, list):
                    secondary.update(
                        accession
                        for accession in secondary_accessions
                        if isinstance(accession, str)
                    )
                entry_type = str(item.get("entryType") or "").casefold()
                if (
                    isinstance(primary_accession, str)
                    and primary_accession
                    and "reviewed" in entry_type
                    and "unreviewed" not in entry_type
                ):
                    reviewed_primary.add(primary_accession)
    except (OSError, ValueError, ijson.JSONError) as error:
        raise SourceValidationError(f"Could not parse UniProt download {path}: {error}") from error

    if records == 0:
        raise SourceValidationError(f"UniProt download {path} contained no result records")
    return AccessionScan(
        records=records,
        primary=frozenset(primary),
        secondary=frozenset(secondary),
        reviewed_primary=frozenset(reviewed_primary),
    )


def validate_reviewed_in_full(full_path: Path, reviewed_path: Path) -> dict[str, int]:
    """Ensure the reviewed human subset is contained in the complete human release."""
    full = _scan_accessions(full_path)
    reviewed = _scan_accessions(reviewed_path)
    full_primary = full.primary
    full_secondary = full.secondary
    reviewed_primary = reviewed.primary
    reviewed_secondary = reviewed.secondary
    missing_primary = sorted(reviewed_primary - full_primary)
    missing_secondary = sorted(reviewed_secondary - (full_primary | full_secondary))

    stats = {
        "full_records": full.records,
        "full_primary_accessions": len(full_primary),
        "full_secondary_accessions": len(full_secondary),
        "full_reviewed_primary_accessions": len(full.reviewed_primary),
        "reviewed_records": reviewed.records,
        "reviewed_primary_accessions": len(reviewed_primary),
        "reviewed_secondary_accessions": len(reviewed_secondary),
        "missing_reviewed_primary_accessions": len(missing_primary),
        "missing_reviewed_secondary_accessions": len(missing_secondary),
    }
    if missing_primary or missing_secondary:
        raise SourceValidationError(
            "UniProt full human download does not contain every reviewed accession: "
            f"{len(missing_primary)} primary and {len(missing_secondary)} secondary missing. "
            f"Primary sample: {', '.join(missing_primary[:20]) or 'none'}. "
            f"Secondary sample: {', '.join(missing_secondary[:20]) or 'none'}."
        )
    return stats


class UniProtHumanSource(HttpSnapshotSource):
    """UniProt's complete and reviewed human JSON streams."""

    _dataset = DatasetId(source="uniprot", dataset="human")

    def __init__(self, http: HttpGateway):
        super().__init__(http)

    @property
    def dataset(self) -> DatasetId:
        return self._dataset

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return UNIPROT_FILES

    @property
    def homepage(self) -> str:
        return UNIPROT_HOMEPAGE

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return UNIPROT_VERSION_STRATEGY

    @property
    def version_check_message(self) -> str:
        return "Checking the UniProt release headers"

    @property
    def validation_message(self) -> str:
        return "Checking that the reviewed records are present in the full dataset"

    @property
    def confirmation_message(self) -> str:
        return "Confirming the UniProt release did not change during download"

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        full = require_download(downloads, UNIPROT_HUMAN_NAME)
        reviewed = require_download(downloads, UNIPROT_REVIEWED_HUMAN_NAME)
        validation = validate_reviewed_in_full(full.resource.path, reviewed.resource.path)
        return SourceValidationResult(
            version=version,
            metadata={
                "version_method": "uniprot_release_headers",
                "validation": {"reviewed_in_full": validation},
            },
        )
