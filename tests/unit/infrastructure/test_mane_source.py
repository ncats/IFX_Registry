"""Contract tests for independently versioned MANE acquisition."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from ifx_registry import FetchRequest, SourceValidationError, VersionProbeRequest
from ifx_registry.domain.errors import VersionMismatchError
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway, HttpMetadata, HttpText
from ifx_registry.infrastructure.sources.mane import (
    MANE_FILE,
    MANE_HEADER,
    ManeHumanSummarySource,
    mane_summary_url,
    parse_mane_version,
)

README = (
    "MANE Version\t1.5\n"
    "NCBI RefSeq Annotation Release\tGCF_000001405.40-RS_2025_08\n"
    "Ensembl Release\t116\n"
)
ROW = [
    "GeneID:1",
    "ENSG00000121410.14",
    "HGNC:5",
    "A1BG",
    "alpha-1-B glycoprotein",
    "NM_130786.4",
    "NP_570602.2",
    "ENST00000263100.8",
    "ENSP00000263100.2",
    "MANE Select",
    "NC_000019.10",
    "58345183",
    "58353492",
    "-",
]


class Gateway(HttpGateway):
    def __init__(self):
        self.payload = gzip.compress(
            ("\t".join(MANE_HEADER) + "\n" + "\t".join(ROW) + "\n").encode()
        )
        self.readme = README
        self.modified = "Thu, 04 Dec 2025 19:53:44 GMT"
        self.downloads: list[str] = []
        self.change_release = False
        self.change_timestamp = False
        self.truncate = False

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        text = (
            self.readme.replace("1.5", "1.6")
            if self.change_release and self.downloads
            else self.readme
        )
        return HttpText(text, HttpMetadata(url, {}))

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        modified = (
            "Fri, 05 Dec 2025 19:53:44 GMT"
            if self.change_timestamp and self.downloads
            else self.modified
        )
        return HttpMetadata(
            url, {"Content-Length": str(len(self.payload)), "Last-Modified": modified}
        )

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        metadata = self.head(url, timeout=timeout)
        self.downloads.append(url)
        destination.write_bytes(self.payload[:-2] if self.truncate else self.payload)
        return DownloadedResource(destination, metadata)


def test_release_metadata_requires_explicit_mane_version() -> None:
    result = parse_mane_version(README)
    assert result.value == "1.5"
    assert result.evidence["ensembl_release"] == "116"
    assert result.version_date is None  # A packaging timestamp is not a release date.
    for text in ("", README.replace("1.5", "../1.5"), README + "MANE Version\t1.6\n"):
        with pytest.raises(SourceValidationError):
            parse_mane_version(text)


def test_fetch_preserves_summary_bytes_and_validates(tmp_path: Path) -> None:
    gateway = Gateway()
    snapshot = ManeHumanSummarySource(gateway, minimum_rows=1).fetch(
        FetchRequest(tmp_path, SourceVersion("1.5")),
    )
    assert snapshot.snapshot_id == "ncbi:mane_human_summary:1.5"
    assert gateway.downloads == [mane_summary_url("1.5")]
    assert len(snapshot.files) == 1
    assert str(snapshot.files[0].relative_path) == MANE_FILE
    assert snapshot.files[0].local_path.read_bytes() == gateway.payload
    assert snapshot.metadata["validation"]["status_counts"] == {"MANE Select": 1}


def test_wrong_pin_stops_before_download(tmp_path: Path) -> None:
    gateway = Gateway()
    with pytest.raises(VersionMismatchError):
        ManeHumanSummarySource(gateway).fetch(FetchRequest(tmp_path, SourceVersion("1.4")))
    assert not gateway.downloads


@pytest.mark.parametrize("change", ["change_release", "change_timestamp", "truncate"])
def test_changed_or_incomplete_acquisition_never_commits(tmp_path: Path, change: str) -> None:
    gateway = Gateway()
    setattr(gateway, change, True)
    with pytest.raises((VersionMismatchError, SourceValidationError)):
        ManeHumanSummarySource(gateway, minimum_rows=1).fetch(FetchRequest(tmp_path))
    assert not (tmp_path / "ncbi/mane_human_summary/1.5").exists()


@pytest.mark.parametrize(
    "column,value",
    [
        ("MANE_status", "unknown"),
        ("#NCBI_GeneID", ""),
        ("Ensembl_nuc", "ENSG123.1"),
        ("GRCh38_chr", "chr1"),
        ("chr_start", "0"),
        ("chr_end", "1"),
        ("chr_strand", "?"),
    ],
)
def test_malformed_rows_rejected(tmp_path: Path, column: str, value: str) -> None:
    gateway = Gateway()
    row = ROW.copy()
    row[MANE_HEADER.index(column)] = value
    gateway.payload = gzip.compress(
        ("\t".join(MANE_HEADER) + "\n" + "\t".join(row) + "\n").encode()
    )
    with pytest.raises(SourceValidationError):
        ManeHumanSummarySource(gateway, minimum_rows=1).fetch(FetchRequest(tmp_path))
    assert not (tmp_path / "ncbi/mane_human_summary/1.5").exists()


@pytest.mark.parametrize(
    "payload", [b"not gzip", gzip.compress(b"wrong\theader\n"), gzip.compress(b"")]
)
def test_bad_gzip_or_header_rejected(tmp_path: Path, payload: bytes) -> None:
    gateway = Gateway()
    gateway.payload = payload
    with pytest.raises(SourceValidationError):
        ManeHumanSummarySource(gateway, minimum_rows=1).fetch(FetchRequest(tmp_path))


def test_small_or_select_free_release_rejected(tmp_path: Path) -> None:
    gateway = Gateway()
    with pytest.raises(SourceValidationError, match="coverage"):
        ManeHumanSummarySource(gateway).fetch(FetchRequest(tmp_path))
    gateway.payload = gzip.compress(
        gzip.decompress(gateway.payload).replace(b"MANE Select", b"MANE Plus Clinical")
    )
    with pytest.raises(SourceValidationError, match="coverage"):
        ManeHumanSummarySource(gateway, minimum_rows=1).fetch(FetchRequest(tmp_path))


def test_missing_probe_metadata_rejected() -> None:
    gateway = Gateway()
    gateway.modified = ""
    with pytest.raises(SourceValidationError, match="metadata"):
        ManeHumanSummarySource(gateway).discover_latest(VersionProbeRequest())


@pytest.mark.parametrize("chromosome", ["NW_009646201.1", "NT_187633.1"])
def test_patch_rows_and_missing_optional_identifiers_are_preserved(
    tmp_path: Path, chromosome: str,
) -> None:
    gateway = Gateway()
    row = ROW.copy()
    row[MANE_HEADER.index("GRCh38_chr")] = chromosome
    for column in ("HGNC_ID", "RefSeq_prot", "Ensembl_prot"):
        row[MANE_HEADER.index(column)] = ""
    gateway.payload = gzip.compress(
        ("\t".join(MANE_HEADER) + "\n" + "\t".join(row) + "\n").encode()
    )
    snapshot = ManeHumanSummarySource(gateway, minimum_rows=1).fetch(FetchRequest(tmp_path))
    assert snapshot.files[0].local_path.read_bytes() == gateway.payload
    assert snapshot.metadata["validation"]["missing_optional_fields"] == {
        "HGNC_ID": 1, "RefSeq_prot": 1, "Ensembl_prot": 1,
    }
