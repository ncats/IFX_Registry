"""Monarch KG discovery and pinned acquisition contract."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from ifx_registry import FetchRequest, SourceValidationError, VersionProbeRequest
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway, HttpMetadata, HttpText
from ifx_registry.infrastructure.sources.monarch import (
    MONARCH_BASE,
    MONARCH_COLUMNS,
    MONARCH_FILE,
    MonarchGeneDiseaseSource,
    monarch_file_url,
    parse_monarch_listing,
)


class Gateway(HttpGateway):
    def __init__(self):
        self.listing = (
            f'<a href="{MONARCH_BASE}/2026-09-04/index.html">2026-09-04</a>'
            f'<a href="{MONARCH_BASE}/2026-10-07/index.html">2026-10-07</a>'
            f'<a href="{MONARCH_BASE}/latest/index.html">latest</a>'
        )
        self.payload = gzip.compress(
            ("\t".join(MONARCH_COLUMNS) + "\n" +
             "\t".join(["HGNC:1", "GENE", "biolink:Gene", "NCBITaxon:9606",
                        "Homo sapiens", "", "biolink:causes", "MONDO:1", "Disease",
                        "biolink:Disease", "", "", "", "infores:omim", ""]) + "\n").encode()
        )
        self.urls: list[str] = []

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        assert url == f"{MONARCH_BASE}/"
        return HttpText(self.listing, HttpMetadata(url, {}))

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        raise AssertionError("Monarch uses the release index, not HEAD dates")

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        self.urls.append(url)
        destination.write_bytes(self.payload)
        return DownloadedResource(destination, HttpMetadata(url, {}))


def test_monarch_release_discovery_pins_file_and_preserves_gzip(tmp_path: Path) -> None:
    gateway = Gateway()
    source = MonarchGeneDiseaseSource(gateway)
    assert source.discover_latest(VersionProbeRequest()).value == "2026-10-07"
    snapshot = source.fetch(FetchRequest(tmp_path, SourceVersion("2026-10-07")))
    assert snapshot.snapshot_id == "monarch:gene_disease_associations:2026-10-07"
    assert gateway.urls == [monarch_file_url("2026-10-07")]
    assert snapshot.files[0].relative_path.as_posix() == MONARCH_FILE
    assert snapshot.files[0].local_path.read_bytes() == gateway.payload
    assert snapshot.metadata["rows"] == 1


def test_monarch_rejects_missing_release_and_corrupt_gzip(tmp_path: Path) -> None:
    with pytest.raises(SourceValidationError, match="no dated release"):
        parse_monarch_listing(f'<a href="{MONARCH_BASE}/latest/index.html">latest</a>')
    gateway = Gateway()
    gateway.payload = gateway.payload[:-4]
    with pytest.raises(SourceValidationError, match="gzip"):
        MonarchGeneDiseaseSource(gateway).fetch(
            FetchRequest(tmp_path, SourceVersion("2026-10-07"))
        )
