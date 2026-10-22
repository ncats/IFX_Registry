"""MedGen acquisition keeps four decompressed files in one dated snapshot."""

from datetime import timedelta
from pathlib import Path
import gzip

import pytest

from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway, HttpMetadata, HttpText
from ifx_registry.infrastructure.sources.ncbi_medgen import MEDGEN_FILES, NcbiMedGenSource


DATE = "Thu, 22 Oct 2026 03:26:53 GMT"


class Gateway(HttpGateway):
    def __init__(self):
        self.payloads = {file.url: (file.name + "|value|\n").encode() for file in MEDGEN_FILES}
        self.download_date = DATE
        self.head_date = DATE
        self.invalid_gzip = False

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        raise AssertionError(f"Unexpected text GET: {url}")

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        return HttpMetadata(url, {"Last-Modified": self.head_date})

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"invalid gzip" if self.invalid_gzip else gzip.compress(self.payloads[url]))
        return DownloadedResource(destination, HttpMetadata(url, {"Last-Modified": self.download_date}))


def test_medgen_fetch_decompresses_four_files_with_dated_version(tmp_path: Path) -> None:
    gateway = Gateway()
    source = NcbiMedGenSource(gateway)
    version = source.discover_latest(VersionProbeRequest())
    assert version.value == "2026-10-22"
    assert len(version.evidence["files"]) == 4

    snapshot = source.fetch(
        FetchRequest(tmp_path, expected_version=SourceVersion("2026-10-22"),
                     timeout=timedelta(seconds=10))
    )
    assert snapshot.snapshot_id == "ncbi:medgen:2026-10-22"
    assert {str(file.relative_path) for file in snapshot.files} == {
        file.name for file in MEDGEN_FILES
    }
    for file in snapshot.files:
        name = str(file.relative_path)
        assert file.local_path.read_bytes() == gateway.payloads[
            next(spec.url for spec in MEDGEN_FILES if spec.name == name)
        ]
        assert file.source_url.endswith(name + ".gz")
        assert file.content_type == "text/plain"


def test_medgen_rejects_changed_file_date(tmp_path: Path) -> None:
    gateway = Gateway()
    gateway.download_date = "Fri, 23 Oct 2026 03:26:53 GMT"
    with pytest.raises(SourceValidationError, match="changed during download"):
        NcbiMedGenSource(gateway).fetch(FetchRequest(tmp_path))
    assert not (tmp_path / "ncbi/medgen/2026-10-22").exists()


def test_medgen_rejects_broken_gzip(tmp_path: Path) -> None:
    gateway = Gateway()
    gateway.invalid_gzip = True
    with pytest.raises(SourceValidationError, match="Could not decompress"):
        NcbiMedGenSource(gateway).fetch(FetchRequest(tmp_path))
    assert not (tmp_path / "ncbi/medgen/2026-10-22").exists()


def test_medgen_requires_last_modified_for_every_file() -> None:
    gateway = Gateway()
    gateway.head_date = ""
    with pytest.raises(SourceValidationError, match="Last-Modified"):
        NcbiMedGenSource(gateway).discover_latest(VersionProbeRequest())
