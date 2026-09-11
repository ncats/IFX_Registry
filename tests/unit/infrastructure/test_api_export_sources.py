"""Contract tests for API-backed and generated source snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import (
    DownloadedResource,
    HttpGateway,
    HttpJson,
    HttpMetadata,
    HttpText,
)
from ifx_registry.infrastructure.sources.api_exports import (
    CURE_FIRST_PAGE,
    CURE_REPORTS_URL,
    GLYGEN_DOWNLOAD_URL,
    GLYGEN_LIST_URL,
    GLYGEN_SEARCH_URL,
    RESOLUTE_URL,
    CureCaseReportsSource,
    DarkKinomeSource,
    GlyGenProteinsSource,
    LinkedOmicsGenesSource,
    ResoluteGenesSource,
)

FIXED_NOW = datetime(2026, 9, 9, 14, 15, 16, tzinfo=UTC)


class FakeApiGateway(HttpGateway):
    def __init__(self) -> None:
        self.json_gets: dict[str, Any] = {}
        self.text_gets: dict[str, str] = {}
        self.json_posts: dict[str, Any] = {}
        self.download_body = b"accession,name\nP12345,Example\n"
        self.post_downloads: list[tuple[str, Mapping[str, Any]]] = []
        self.json_get_headers: list[Mapping[str, str]] = []

    @staticmethod
    def _metadata(url: str, content_type: str = "application/json") -> HttpMetadata:
        return HttpMetadata(url, {"Content-Type": content_type})

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        return HttpText(self.text_gets[url], self._metadata(url, "text/html"))

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        raise AssertionError(f"Unexpected HEAD {url}")

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        raise AssertionError(f"Unexpected download {url}")

    def get_json(
        self,
        url: str,
        *,
        timeout: float,
        headers: Mapping[str, str] | None = None,
    ) -> HttpJson:
        self.json_get_headers.append(dict(headers or {}))
        return HttpJson(self.json_gets[url], self._metadata(url))

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> HttpJson:
        return HttpJson(self.json_posts[url], self._metadata(url))

    def post_download(
        self,
        url: str,
        payload: Mapping[str, Any],
        destination: Path,
        *,
        timeout: float,
        accept: str | None = None,
    ) -> DownloadedResource:
        self.post_downloads.append((url, payload))
        destination.write_bytes(self.download_body)
        return DownloadedResource(destination, self._metadata(url, accept or "text/csv"))


def test_cure_exports_every_page_under_reserved_capture_version(tmp_path: Path) -> None:
    gateway = FakeApiGateway()
    delays: list[float] = []
    second_page = f"{CURE_REPORTS_URL}?page=2"
    gateway.json_gets = {
        CURE_FIRST_PAGE: {"count": 2, "results": [{"id": 1}], "next": second_page},
        second_page: {"count": 2, "results": [{"id": 2}], "next": None},
    }
    source = CureCaseReportsSource(
        gateway,
        "test-cure-key",
        clock=lambda: FIXED_NOW,
        sleeper=delays.append,
    )

    version = source.discover_latest(VersionProbeRequest())
    snapshot = source.fetch(
        FetchRequest(tmp_path, expected_version=SourceVersion(version.value))
    )

    assert version.value == "reports_20260909T141516Z"
    assert snapshot.version == version
    assert snapshot.version.version_date == FIXED_NOW.date()
    assert snapshot.metadata["record_count"] == 2
    assert snapshot.metadata["page_count"] == 2
    assert snapshot.files[0].local_path.read_text().splitlines() == [
        '{"id": 1}',
        '{"id": 2}',
    ]
    assert gateway.json_get_headers == [
        {"X-API-Key": "test-cure-key"},
        {"X-API-Key": "test-cure-key"},
        {"X-API-Key": "test-cure-key"},
    ]
    assert delays == [0.5]


def test_glygen_uses_listcache_id_for_check_and_download(tmp_path: Path) -> None:
    gateway = FakeApiGateway()
    gateway.json_posts = {
        GLYGEN_SEARCH_URL: {"list_id": "search-1", "resultcount": 1},
        GLYGEN_LIST_URL: {"cache_info": {"listcache_id": "cache-42"}},
    }
    source = GlyGenProteinsSource(gateway, clock=lambda: FIXED_NOW)

    version = source.discover_latest(VersionProbeRequest())
    snapshot = source.fetch(FetchRequest(tmp_path, expected_version=version))

    assert version.value == "cache-42"
    assert snapshot.metadata["record_count"] == 1
    assert gateway.post_downloads == [
        (
            GLYGEN_DOWNLOAD_URL,
            {
                "id": "cache-42",
                "download_type": "protein_list",
                "format": "csv",
                "compressed": False,
            },
        )
    ]


def test_dark_kinome_capture_deduplicates_links(tmp_path: Path) -> None:
    gateway = FakeApiGateway()
    gateway.text_gets["https://darkkinome.org/data"] = (
        '<a href="/kinase/AAK1">AAK1</a><a href="/kinase/AAK1">duplicate</a>'
        '<a href="/kinase/BMP2K">BMP2K</a>'
    )
    source = DarkKinomeSource(gateway, clock=lambda: FIXED_NOW)
    version = source.discover_latest(VersionProbeRequest())

    snapshot = source.fetch(FetchRequest(tmp_path, expected_version=version))

    assert version.value == "capture_20260909T141516Z"
    assert snapshot.version.version_date == FIXED_NOW.date()
    assert snapshot.metadata["record_count"] == 2
    assert snapshot.files[0].local_path.read_text().splitlines() == [
        "symbol\turl",
        "AAK1\thttps://darkkinome.org/kinase/AAK1",
        "BMP2K\thttps://darkkinome.org/kinase/BMP2K",
    ]


def test_resolute_capture_flattens_gene_identifiers(tmp_path: Path) -> None:
    gateway = FakeApiGateway()
    gateway.json_posts[RESOLUTE_URL] = {
        "data": {
            "genesList": [
                {
                    "symbol": "SLC1A1",
                    "proteinsList": [
                        {
                            "nextprotac": "NX_P43005",
                            "identifiersList": [{"identifier": "ENSP00000262352"}],
                        }
                    ],
                }
            ]
        }
    }
    source = ResoluteGenesSource(gateway, clock=lambda: FIXED_NOW)
    version = source.discover_latest(VersionProbeRequest())

    snapshot = source.fetch(FetchRequest(tmp_path, expected_version=version))

    assert snapshot.metadata["record_count"] == 1
    assert "NX_P43005\tENSP00000262352" in snapshot.files[0].local_path.read_text()


def test_linkedomics_capture_writes_unique_gene_rows(tmp_path: Path) -> None:
    gateway = FakeApiGateway()
    url = "https://kb.linkedomics.org/data/list/gene"
    gateway.json_gets[url] = ["TP53", "BRCA1", "TP53", ""]
    source = LinkedOmicsGenesSource(gateway, clock=lambda: FIXED_NOW)
    version = source.discover_latest(VersionProbeRequest())

    snapshot = source.fetch(FetchRequest(tmp_path, expected_version=version))

    assert snapshot.metadata["record_count"] == 2
    assert snapshot.files[0].local_path.read_text().splitlines() == [
        "symbol\turl",
        "TP53\thttps://kb.linkedomics.org/gene/TP53",
        "BRCA1\thttps://kb.linkedomics.org/gene/BRCA1",
    ]


def test_linkedomics_rejects_non_string_gene_symbols() -> None:
    gateway = FakeApiGateway()
    gateway.json_gets["https://kb.linkedomics.org/data/list/gene"] = ["TP53", None]
    source = LinkedOmicsGenesSource(gateway, clock=lambda: FIXED_NOW)

    with pytest.raises(SourceValidationError, match="non-string"):
        source.discover_latest(VersionProbeRequest())


def test_cure_rejects_cross_origin_pagination(tmp_path: Path) -> None:
    gateway = FakeApiGateway()
    gateway.json_gets[CURE_FIRST_PAGE] = {
        "count": 1,
        "results": [{"id": 1}],
        "next": "https://attacker.example/reports?page=2",
    }
    source = CureCaseReportsSource(gateway, "test-cure-key", clock=lambda: FIXED_NOW)
    version = source.discover_latest(VersionProbeRequest())

    with pytest.raises(SourceValidationError, match="unsafe next URL"):
        source.fetch(FetchRequest(tmp_path, expected_version=version))


def test_resolute_rejects_changed_nested_collection_shape(tmp_path: Path) -> None:
    gateway = FakeApiGateway()
    gateway.json_posts[RESOLUTE_URL] = {
        "data": {"genesList": [{"symbol": "SLC1A1", "proteinsList": {}}]}
    }
    source = ResoluteGenesSource(gateway, clock=lambda: FIXED_NOW)
    version = source.discover_latest(VersionProbeRequest())

    with pytest.raises(SourceValidationError, match="proteinsList was not a list"):
        source.fetch(FetchRequest(tmp_path, expected_version=version))
