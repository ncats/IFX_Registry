"""The standalone MedGen gene association file has its own Registry dataset."""

from pathlib import Path

import pytest

from ifx_registry.application.contracts import FetchRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway, HttpMetadata, HttpText
from ifx_registry.infrastructure.sources.ncbi_mim2gene_medgen import (
    MIM2GENE_MEDGEN_HEADER,
    MIM2GENE_MEDGEN_NAME,
    MIM2GENE_MEDGEN_URL,
    NcbiMim2geneMedgenSource,
    _validate_file,
)


DATE = "Thu, 22 Oct 2026 09:05:03 GMT"


class Gateway(HttpGateway):
    def __init__(self):
        self.head_date = DATE
        self.download_date = DATE
        self.contents = ("\t".join(MIM2GENE_MEDGEN_HEADER) + "\n" +
                         "100050\t-\tphenotype\t-\tC3149220\t-\n" * 10_000)

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        raise AssertionError(f"Unexpected text GET: {url}")

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        assert url == MIM2GENE_MEDGEN_URL
        return HttpMetadata(url, {"Last-Modified": self.head_date})

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        assert url == MIM2GENE_MEDGEN_URL
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.contents)
        return DownloadedResource(destination, HttpMetadata(url, {"Last-Modified": self.download_date}))


def test_mim2gene_medgen_fetches_own_dated_snapshot(tmp_path: Path) -> None:
    gateway = Gateway()
    source = NcbiMim2geneMedgenSource(gateway)
    snapshot = source.fetch(FetchRequest(
        tmp_path, expected_version=SourceVersion("2026-10-22")
    ))
    assert snapshot.snapshot_id == "ncbi:mim2gene_medgen:2026-10-22"
    assert [str(file.relative_path) for file in snapshot.files] == [MIM2GENE_MEDGEN_NAME]
    assert snapshot.files[0].local_path.read_text() == gateway.contents
    assert snapshot.metadata["rows"] == 10_000


def test_mim2gene_medgen_rejects_changed_timestamp(tmp_path: Path) -> None:
    gateway = Gateway()
    gateway.download_date = "Fri, 23 Oct 2026 09:05:03 GMT"
    with pytest.raises(SourceValidationError, match="changed during download"):
        NcbiMim2geneMedgenSource(gateway).fetch(FetchRequest(tmp_path))
    assert not (tmp_path / "ncbi/mim2gene_medgen/2026-10-22").exists()


@pytest.mark.parametrize("bad_row", ["bad\t-\tphenotype\t-\tC1\t-", "100050\t-\tother\t-\tC1\t-"])
def test_mim2gene_medgen_rejects_malformed_rows(tmp_path: Path, bad_row: str) -> None:
    path = tmp_path / MIM2GENE_MEDGEN_NAME
    path.write_text("\t".join(MIM2GENE_MEDGEN_HEADER) + "\n" + bad_row + "\n")
    with pytest.raises(SourceValidationError, match="Malformed"):
        _validate_file(path)
