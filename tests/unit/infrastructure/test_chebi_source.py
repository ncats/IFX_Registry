from __future__ import annotations

import gzip

import pytest

from ifx_registry import FetchRequest, VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError, VersionMismatchError
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway, HttpMetadata, HttpText
from ifx_registry.infrastructure.sources.chebi import (
    CHEBI_README_URL,
    ChebiFullOntologySource,
    parse_chebi_release,
)


class ChebiGateway(HttpGateway):
    def __init__(self):
        self.dates = ["2026-09-09", "2026-09-09"]
        self.release = "255"
        self.downloads = []

    def get_text(self, url, *, timeout):
        assert url == CHEBI_README_URL
        return HttpText(
            f"ChEBI Release: 255\nDate of last update: {self.dates.pop(0)}\n",
            HttpMetadata(url, {}),
        )

    def head(self, url, *, timeout):
        raise AssertionError("Discovery needs only the README")

    def download(self, url, destination, *, timeout):
        self.downloads.append(url)
        with gzip.open(destination, "wt") as handle:
            handle.write(f"data-version: {self.release}\ndate: 09:09:2026 21:03\n")
        return DownloadedResource(destination, HttpMetadata(url, {}))


def test_discovery_distinguishes_rebuilds_without_downloading_ontology():
    gateway = ChebiGateway()
    gateway.dates = ["2026-09-01", "2026-09-09"]
    source = ChebiFullOntologySource(gateway)
    first = source.discover_latest(VersionProbeRequest())
    second = source.discover_latest(VersionProbeRequest())
    assert first.value == "255-2026-09-01"
    assert second.value == "255-2026-09-09"
    assert second.version_date.isoformat() == "2026-09-09"
    assert second.evidence["readme_release"] == "255"
    assert not gateway.downloads


def test_acquisition_validates_original_release_and_preserves_legacy_snapshot(tmp_path):
    legacy = tmp_path / "chebi" / "ontology_full" / "255"
    legacy.mkdir(parents=True)
    sentinel = legacy / "existing"
    sentinel.write_text("immutable")
    result = ChebiFullOntologySource(ChebiGateway()).fetch(FetchRequest(tmp_path))
    assert result.snapshot_id == "chebi:ontology_full:255-2026-09-09"
    assert result.version.evidence["obo_data_version"] == "255"
    assert result.version.evidence["obo_date"] == "2026-09-09"
    assert sentinel.read_text() == "immutable"


@pytest.mark.parametrize("failure", ["release", "rebuild"])
def test_acquisition_rejects_wrong_release_or_mid_download_rebuild(tmp_path, failure):
    gateway = ChebiGateway()
    if failure == "release":
        gateway.release = "254"
    else:
        gateway.dates[-1] = "2026-09-10"
    with pytest.raises((SourceValidationError, VersionMismatchError)):
        ChebiFullOntologySource(gateway).fetch(FetchRequest(tmp_path))
    assert not (tmp_path / "chebi" / "ontology_full" / "255-2026-09-09").exists()


@pytest.mark.parametrize(
    "text", ["ChEBI Release: 255", "ChEBI Release: 255\nDate of last update: 2026-99-99"]
)
def test_invalid_readme_fails_with_source_error(text):
    with pytest.raises(SourceValidationError):
        parse_chebi_release(text)
