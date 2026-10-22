"""Babel disease compendium stays raw, complete, and release pinned."""

from pathlib import Path

import pytest

from ifx_registry import FetchRequest, SourceValidationError
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway, HttpMetadata, HttpText
from ifx_registry.infrastructure.sources.babel import BABEL_ROOT_URL
from ifx_registry.infrastructure.sources.babel_disease import BabelDiseaseCompendiumSource


class Gateway(HttpGateway):
    def __init__(self):
        self.listing = '<a href="2026jul22/">release</a>'
        self.payload = (
            b'{"type":"biolink:Disease","identifiers":[{"i":"MONDO:1"}],'
            b'"preferred_name":"One","taxa":[]}\n'
            b'{"type":"biolink:Disease","identifiers":[{"i":"MONDO:2"}],'
            b'"preferred_name":"Two","taxa":["NCBITaxon:9606"]}\n'
        )
        self.downloaded_urls: list[str] = []

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        assert url == BABEL_ROOT_URL
        return HttpText(self.listing, HttpMetadata(url, {}))

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        raise AssertionError("HEAD is not used")

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        self.downloaded_urls.append(url)
        destination.write_bytes(self.payload)
        return DownloadedResource(
            destination, HttpMetadata(url, {"Content-Length": str(len(self.payload))})
        )


def test_babel_disease_fetch_preserves_all_records(tmp_path: Path) -> None:
    gateway = Gateway()
    source = BabelDiseaseCompendiumSource(gateway, minimum_records=2)
    snapshot = source.fetch(FetchRequest(tmp_path, SourceVersion("2026jul22")))
    assert snapshot.snapshot_id == "babel:disease_compendium:2026jul22"
    assert snapshot.files[0].relative_path.as_posix() == "nodenorm_disease.txt"
    assert snapshot.files[0].local_path.read_bytes() == gateway.payload
    assert snapshot.metadata["disease_records"] == 2
    assert gateway.downloaded_urls == [
        f"{BABEL_ROOT_URL}2026jul22/compendia/Disease.txt"
    ]


def test_babel_disease_rejects_malformed_record(tmp_path: Path) -> None:
    gateway = Gateway()
    gateway.payload = b'{"type":"biolink:Gene","identifiers":[{"i":"MONDO:1"}],"taxa":[]}\n'
    with pytest.raises(SourceValidationError, match="malformed"):
        BabelDiseaseCompendiumSource(gateway, minimum_records=1).fetch(
            FetchRequest(tmp_path, SourceVersion("2026jul22"))
        )
