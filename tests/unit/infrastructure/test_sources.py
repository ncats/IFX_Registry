"""Contract-focused tests for built-in source adapters."""

from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from ifx_registry import FetchRequest, FetchSource, SourceValidationError, VersionProbeRequest
from ifx_registry.domain.errors import VersionMismatchError
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import (
    DownloadedResource,
    HttpGateway,
    HttpMetadata,
    HttpText,
)
from ifx_registry.infrastructure.sources.inspected_files import (
    INSPECTED_FILE_SOURCES,
    InspectedFileSource,
)
from ifx_registry.infrastructure.sources.last_modified import (
    LAST_MODIFIED_SOURCES,
    LastModifiedHttpSource,
)
from ifx_registry.infrastructure.sources.reactome import (
    REACTOME_FILES,
    REACTOME_VERSION_URL,
    ReactomePathwaysSource,
)
from ifx_registry.infrastructure.sources.uniprot import (
    UNIPROT_HUMAN_URL,
    UNIPROT_RELEASE_PROBE_URL,
    UNIPROT_REVIEWED_HUMAN_URL,
    UniProtHumanSource,
    normalize_uniprot_release_date,
    validate_reviewed_in_full,
)


class FakeHttpGateway(HttpGateway):
    def __init__(self) -> None:
        self.text_responses: dict[str, HttpText] = {}
        self.head_responses: dict[str, HttpMetadata] = {}
        self.payloads: dict[str, bytes] = {}
        self.download_headers: dict[str, dict[str, str]] = {}
        self.download_calls: list[tuple[str, Path, float]] = []

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        return self.text_responses[url]

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        return self.head_responses[url]

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        self.download_calls.append((url, destination, timeout))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payloads[url])
        return DownloadedResource(
            path=destination,
            metadata=HttpMetadata(
                final_url=url,
                headers=self.download_headers.get(
                    url,
                    {"content-type": "application/octet-stream"},
                ),
            ),
        )


def _gzip_uniprot(records: list[dict[str, object]]) -> bytes:
    return gzip.compress(json.dumps({"results": records}).encode())


def _reactome_gateway(
    *,
    last_modified: str | None = "Sat, 20 Jun 2026 00:20:37 GMT",
) -> FakeHttpGateway:
    gateway = FakeHttpGateway()
    gateway.text_responses[REACTOME_VERSION_URL] = HttpText(
        text="97\n",
        metadata=HttpMetadata(REACTOME_VERSION_URL, {"content-type": "text/plain"}),
    )
    for file_spec in REACTOME_FILES:
        gateway.payloads[file_spec.url] = f"contents of {file_spec.name}\n".encode()
        gateway.download_headers[file_spec.url] = {"content-type": "text/plain"}
    if last_modified:
        gateway.download_headers[REACTOME_FILES[-1].url]["Last-Modified"] = last_modified
    return gateway


def _uniprot_gateway(
    *,
    full_records: list[dict[str, object]],
    reviewed_records: list[dict[str, object]],
) -> FakeHttpGateway:
    gateway = FakeHttpGateway()
    gateway.head_responses[UNIPROT_RELEASE_PROBE_URL] = HttpMetadata(
        final_url=UNIPROT_RELEASE_PROBE_URL,
        headers={
            "X-UniProt-Release": "2026_03",
            "X-UniProt-Release-Date": "02-September-2026",
        },
    )
    gateway.payloads[UNIPROT_HUMAN_URL] = _gzip_uniprot(full_records)
    gateway.payloads[UNIPROT_REVIEWED_HUMAN_URL] = _gzip_uniprot(reviewed_records)
    gateway.download_headers[UNIPROT_HUMAN_URL] = {"content-type": "application/x-gzip"}
    gateway.download_headers[UNIPROT_REVIEWED_HUMAN_URL] = {"content-type": "application/x-gzip"}
    return gateway


def test_reactome_discovers_database_version() -> None:
    source = ReactomePathwaysSource(_reactome_gateway())

    version = source.discover_latest(VersionProbeRequest(timeout=timedelta(seconds=9)))

    assert version.value == "97"
    assert version.evidence["method"] == "reactome_database_version"


def test_shared_last_modified_source_uses_newest_file_and_stable_names(
    tmp_path: Path,
) -> None:
    definition = LAST_MODIFIED_SOURCES["ncbi_publications"]
    gateway = FakeHttpGateway()
    dates = (
        "Tue, 01 Sep 2026 08:00:00 GMT",
        "Wed, 02 Sep 2026 08:00:00 GMT",
    )
    for file_spec, modified in zip(definition.files, dates, strict=True):
        gateway.head_responses[file_spec.url] = HttpMetadata(
            file_spec.url,
            {"Last-Modified": modified},
        )
        gateway.payloads[file_spec.url] = f"contents of {file_spec.name}\n".encode()

    source = LastModifiedHttpSource(gateway, definition)
    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.version.value == "2026-09-02"
    assert snapshot.version.version_date == date(2026, 9, 2)
    assert [str(file.relative_path) for file in snapshot.files] == [
        "gene2pubmed.gz",
        "generifs_basic.gz",
    ]
    assert snapshot.version.evidence["method"] == "multi_file_max_last_modified"


def test_inspected_file_source_uses_embedded_ctd_report_date(tmp_path: Path) -> None:
    definition = INSPECTED_FILE_SOURCES["ctd_curated_genes_diseases"]
    gateway = FakeHttpGateway()
    gateway.payloads[definition.file.url] = gzip.compress(
        b"# Report created: Tue May 28 10:15:30 EDT 2026\nGeneSymbol\tDiseaseName\n"
    )
    source = InspectedFileSource(gateway, definition)

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.version.value == "2026-05-28"
    assert snapshot.version.version_date == date(2026, 5, 28)
    assert snapshot.files[0].local_path.read_bytes() == gateway.payloads[definition.file.url]


def test_reactome_fetches_complete_versioned_snapshot(tmp_path: Path) -> None:
    gateway = _reactome_gateway()
    source = ReactomePathwaysSource(gateway)

    snapshot = FetchSource().execute(source, FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "reactome:pathways:97"
    assert snapshot.version.version_date == date(2026, 6, 20)
    assert {str(file.relative_path) for file in snapshot.files} == {
        file_spec.name for file_spec in REACTOME_FILES
    }
    assert all(
        file.local_path.parent == tmp_path / "reactome" / "pathways" / "97"
        for file in snapshot.files
    )
    assert len(gateway.download_calls) == 5


def test_reactome_does_not_publish_snapshot_without_release_date(tmp_path: Path) -> None:
    source = ReactomePathwaysSource(_reactome_gateway(last_modified=None))

    with pytest.raises(SourceValidationError, match="Last-Modified"):
        source.fetch(FetchRequest(tmp_path))

    assert not (tmp_path / "reactome" / "pathways" / "97").exists()


def test_reactome_rejects_moved_upstream_before_downloading(tmp_path: Path) -> None:
    gateway = _reactome_gateway()
    source = ReactomePathwaysSource(gateway)

    with pytest.raises(VersionMismatchError, match="upstream currently reports 97"):
        source.fetch(FetchRequest(tmp_path, expected_version=SourceVersion("96")))

    assert gateway.download_calls == []


def test_reactome_rejects_release_that_moves_during_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _reactome_gateway()
    responses = iter(
        [
            gateway.text_responses[REACTOME_VERSION_URL],
            HttpText("98", HttpMetadata(REACTOME_VERSION_URL, {})),
        ]
    )
    monkeypatch.setattr(gateway, "get_text", lambda url, timeout: next(responses))
    source = ReactomePathwaysSource(gateway)

    with pytest.raises(VersionMismatchError, match="upstream currently reports 98"):
        source.fetch(FetchRequest(tmp_path))

    assert len(gateway.download_calls) == 5
    assert not (tmp_path / "reactome" / "pathways" / "97").exists()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("02-September-2026", date(2026, 9, 2)),
        ("02-Sep-26", date(2026, 9, 2)),
        ("2026-09-02", date(2026, 9, 2)),
        (None, None),
    ],
)
def test_normalize_uniprot_release_date(raw: str | None, expected: date | None) -> None:
    assert normalize_uniprot_release_date(raw) == expected


def test_uniprot_fetches_and_validates_human_files(tmp_path: Path) -> None:
    full_records: list[dict[str, object]] = [
        {
            "primaryAccession": "P00001",
            "secondaryAccessions": ["S00001"],
            "entryType": "UniProtKB reviewed (Swiss-Prot)",
        },
        {"primaryAccession": "P00002", "entryType": "UniProtKB unreviewed (TrEMBL)"},
    ]
    reviewed_records: list[dict[str, object]] = [
        {
            "primaryAccession": "P00001",
            "secondaryAccessions": ["S00001"],
            "entryType": "UniProtKB reviewed (Swiss-Prot)",
        }
    ]
    gateway = _uniprot_gateway(full_records=full_records, reviewed_records=reviewed_records)
    source = UniProtHumanSource(gateway)

    snapshot = FetchSource().execute(source, FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "uniprot:human:2026_03"
    assert snapshot.version.version_date == date(2026, 9, 2)
    assert [str(file.relative_path) for file in snapshot.files] == [
        "uniprot-human.json.gz",
        "uniprot-human-reviewed.json.gz",
    ]
    stats = snapshot.metadata["validation"]["reviewed_in_full"]
    assert stats["full_records"] == 2
    assert stats["reviewed_records"] == 1
    assert stats["missing_reviewed_primary_accessions"] == 0


def test_uniprot_rejects_inconsistent_download_without_publishing(tmp_path: Path) -> None:
    gateway = _uniprot_gateway(
        full_records=[{"primaryAccession": "P00001"}],
        reviewed_records=[{"primaryAccession": "P99999"}],
    )
    source = UniProtHumanSource(gateway)

    with pytest.raises(SourceValidationError, match="1 primary"):
        source.fetch(FetchRequest(tmp_path))

    assert not (tmp_path / "uniprot" / "human" / "2026_03").exists()


def test_uniprot_validator_accepts_reviewed_secondary_as_full_primary(tmp_path: Path) -> None:
    full_path = tmp_path / "full.json.gz"
    reviewed_path = tmp_path / "reviewed.json.gz"
    full_path.write_bytes(_gzip_uniprot([{"primaryAccession": "P00001"}]))
    reviewed_path.write_bytes(
        _gzip_uniprot([{"primaryAccession": "P00001", "secondaryAccessions": ["P00001"]}])
    )

    stats = validate_reviewed_in_full(full_path, reviewed_path)

    assert stats["missing_reviewed_secondary_accessions"] == 0


def test_uniprot_validator_rejects_payload_without_results(tmp_path: Path) -> None:
    full_path = tmp_path / "full.json.gz"
    reviewed_path = tmp_path / "reviewed.json.gz"
    full_path.write_bytes(gzip.compress(b'{"messages": ["upstream error"]}'))
    reviewed_path.write_bytes(_gzip_uniprot([{"primaryAccession": "P00001"}]))

    with pytest.raises(SourceValidationError, match="contained no result records"):
        validate_reviewed_in_full(full_path, reviewed_path)
