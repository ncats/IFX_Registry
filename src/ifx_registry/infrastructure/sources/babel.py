"""Human-focused snapshots generated from versioned Babel compendia."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.application.progress import ProgressUpdate
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.api_exports import (
    GeneratedArtifact,
    GeneratedSnapshotSource,
)

BABEL_ROOT_URL = "https://stars.renci.org/var/babel_outputs/"
_VERSION_PATTERN = re.compile(r'href="(\d{4}[a-z]{3}\d{1,2})/"')
_LISTING_PATTERN = re.compile(
    r'href="(?P<name>[^"/]+)"[^>]*>[^<]*</a>\s+'
    r'(?P<date>\d{2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2})\s+'
    r'(?P<size>\d+)'
)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass(frozen=True, slots=True)
class BabelCompendiumDefinition:
    dataset: DatasetId
    prefix: str
    output_name: str
    minimum_human_records: int


BABEL_HUMAN_GENE = BabelCompendiumDefinition(
    DatasetId("babel", "human_gene_compendium"),
    "Gene.txt",
    "nodenorm_genes.jsonl",
    60_000,
)
BABEL_HUMAN_PROTEIN = BabelCompendiumDefinition(
    DatasetId("babel", "human_protein_compendium"),
    "Protein.txt",
    "nodenorm_proteins.jsonl",
    200_000,
)


@dataclass(frozen=True, slots=True)
class BabelListedFile:
    name: str
    modified: str
    size_bytes: int


class BabelHumanCompendiumSource(GeneratedSnapshotSource):
    """Stream one immutable Babel release and retain its human JSON records."""

    content_type = "application/x-ndjson"

    def __init__(
        self,
        http: HttpGateway,
        definition: BabelCompendiumDefinition,
        *,
        root_url: str = BABEL_ROOT_URL,
    ):
        super().__init__(http)
        self._definition = definition
        self._root_url = root_url.rstrip("/") + "/"
        self.file_name = definition.output_name

    @property
    def dataset(self) -> DatasetId:
        return self._definition.dataset

    @property
    def homepage(self) -> str:
        return "https://github.com/TranslatorSRI/Babel"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (self._root_url,)

    @property
    def version_check_description(self) -> str:
        return (
            "Selects the newest date-named Babel release directory and records the "
            "complete chunk listing before and after acquisition."
        )

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return (self._root_url,)

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        response = self._http.get_text(
            self._root_url,
            timeout=request.timeout.total_seconds(),
        )
        candidates = {
            value: _babel_version_date(value)
            for value in _VERSION_PATTERN.findall(response.text)
        }
        if not candidates:
            raise SourceValidationError(
                f"Babel release listing {self._root_url} contained no dated releases"
            )
        version = max(candidates, key=candidates.__getitem__)
        version_date = candidates[version]
        return SourceVersion(
            version,
            version_date=version_date,
            evidence={
                "method": "newest_versioned_babel_directory",
                "listing_url": response.metadata.final_url,
                "release_url": self._release_url(version),
                "candidate_count": len(candidates),
            },
        )

    def _version_for_fetch(self, request: FetchRequest) -> SourceVersion:
        return self.discover_latest(VersionProbeRequest(timeout=request.timeout))

    def _generate(
        self,
        request: FetchRequest,
        version: SourceVersion,
        destination: Path,
    ) -> GeneratedArtifact:
        release_url = self._release_url(version.value)
        listing_url = urljoin(release_url, "compendia/")
        before = self._list_compendium_files(listing_url, request)
        selected_files = _select_complete_file_set(before, self._definition.prefix)
        total = len(selected_files)
        source_records = 0
        human_records = 0
        observed_files: list[dict[str, object]] = []
        digest = hashlib.sha256()

        with destination.open("wb") as output:
            for index, listed in enumerate(selected_files, start=1):
                request.progress.report(
                    ProgressUpdate(
                        "downloading",
                        f"Filtering {listed.name} for human records",
                        index,
                        total,
                    )
                )
                chunk_url = urljoin(listing_url, listed.name)
                chunk_path = destination.with_name(f".{listed.name}.download")
                chunk_path.unlink(missing_ok=True)
                try:
                    resource = self._http.download(
                        chunk_url,
                        chunk_path,
                        timeout=request.timeout.total_seconds(),
                    )
                    observed_size = chunk_path.stat().st_size
                    if observed_size != listed.size_bytes:
                        raise SourceValidationError(
                            f"Babel {listed.name} size changed: listing declared "
                            f"{listed.size_bytes}, downloaded {observed_size}"
                        )
                    chunk_digest = hashlib.sha256()
                    with chunk_path.open("rb") as handle:
                        for line_number, line in enumerate(handle, start=1):
                            chunk_digest.update(line)
                            digest.update(line)
                            if not line.strip():
                                continue
                            source_records += 1
                            try:
                                record = json.loads(line)
                            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                                raise SourceValidationError(
                                    f"Babel {listed.name} line {line_number} is not valid JSON"
                                ) from error
                            if not isinstance(record, Mapping):
                                raise SourceValidationError(
                                    f"Babel {listed.name} line {line_number} is not an object"
                                )
                            taxa = record.get("taxa")
                            if not isinstance(taxa, list) or not all(
                                isinstance(item, str) for item in taxa
                            ):
                                raise SourceValidationError(
                                    f"Babel {listed.name} line {line_number} has invalid taxa"
                                )
                            if "NCBITaxon:9606" in taxa:
                                if not line.endswith((b"\n", b"\r")):
                                    raise SourceValidationError(
                                        f"Babel {listed.name} line {line_number} has no newline"
                                    )
                                output.write(line)
                                human_records += 1
                    observed_files.append(
                        {
                            "name": listed.name,
                            "url": chunk_url,
                            "final_url": resource.metadata.final_url,
                            "listing_modified": listed.modified,
                            "declared_size_bytes": listed.size_bytes,
                            "observed_size_bytes": observed_size,
                            "etag": resource.metadata.header("ETag"),
                            "last_modified": resource.metadata.header("Last-Modified"),
                            "sha256": chunk_digest.hexdigest(),
                        }
                    )
                finally:
                    chunk_path.unlink(missing_ok=True)

        if human_records < self._definition.minimum_human_records:
            raise SourceValidationError(
                f"Babel {self._definition.prefix} human record count is below the "
                f"reviewed minimum: {human_records} < "
                f"{self._definition.minimum_human_records}"
            )
        after = self._list_compendium_files(listing_url, request)
        after_selected = _select_complete_file_set(after, self._definition.prefix)
        if after_selected != selected_files:
            raise SourceValidationError(
                f"Babel {self._definition.prefix} listing changed during acquisition"
            )
        confirmed = self.discover_latest(VersionProbeRequest(timeout=request.timeout))
        if confirmed.value != version.value:
            raise SourceValidationError(
                f"Babel latest release changed from {version.value} to {confirmed.value} "
                "during acquisition"
            )

        return GeneratedArtifact(
            source_url=listing_url,
            upstream_urls=tuple(
                urljoin(listing_url, item.name) for item in selected_files
            ),
            metadata={
                "release": version.value,
                "taxon_id": 9606,
                "selection": "semantic_taxa_membership",
                "source_records": source_records,
                "human_records": human_records,
                "minimum_human_records": self._definition.minimum_human_records,
                "source_stream_sha256": digest.hexdigest(),
                "files": observed_files,
            },
        )

    def _release_url(self, version: str) -> str:
        return urljoin(self._root_url, f"{version}/")

    def _list_compendium_files(
        self,
        listing_url: str,
        request: FetchRequest,
    ) -> tuple[BabelListedFile, ...]:
        response = self._http.get_text(
            listing_url,
            timeout=request.timeout.total_seconds(),
        )
        return tuple(
            BabelListedFile(
                match.group("name"),
                match.group("date"),
                int(match.group("size")),
            )
            for match in _LISTING_PATTERN.finditer(response.text)
            if match.group("name").startswith(self._definition.prefix)
        )


def _babel_version_date(value: str) -> date:
    match = re.fullmatch(r"(\d{4})([a-z]{3})(\d{1,2})", value)
    if match is None or match.group(2) not in _MONTHS:
        raise SourceValidationError(f"Invalid Babel release name: {value}")
    return date(int(match.group(1)), _MONTHS[match.group(2)], int(match.group(3)))


def _select_complete_file_set(
    files: tuple[BabelListedFile, ...],
    prefix: str,
) -> tuple[BabelListedFile, ...]:
    chunks: dict[int, BabelListedFile] = {}
    chunk_pattern = re.compile(rf"{re.escape(prefix)}\.(\d+)$")
    monolith: BabelListedFile | None = None
    for item in files:
        if item.name == prefix:
            monolith = item
            continue
        match = chunk_pattern.fullmatch(item.name)
        if match is not None:
            chunks[int(match.group(1))] = item
    if chunks:
        indexes = sorted(chunks)
        expected = list(range(indexes[-1] + 1))
        if indexes != expected:
            raise SourceValidationError(
                f"Babel {prefix} chunks are not contiguous: {indexes}"
            )
        return tuple(chunks[index] for index in indexes)
    if monolith is not None:
        return (monolith,)
    raise SourceValidationError(f"Babel listing contains no {prefix} files")


def build_babel_gene_source(http: HttpGateway) -> BabelHumanCompendiumSource:
    return BabelHumanCompendiumSource(http, BABEL_HUMAN_GENE)


def build_babel_protein_source(http: HttpGateway) -> BabelHumanCompendiumSource:
    return BabelHumanCompendiumSource(http, BABEL_HUMAN_PROTEIN)
