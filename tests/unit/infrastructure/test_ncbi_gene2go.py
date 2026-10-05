from __future__ import annotations

import gzip

import pytest

from ifx_registry import FetchRequest, VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError, VersionMismatchError
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway, HttpMetadata
from ifx_registry.infrastructure.sources.ncbi_gene2go import GENE2GO_HEADER, NcbiGene2GoSource

DATE = "Mon, 05 Oct 2026 05:28:42 GMT"
HUMAN = "9606\t1\tGO:0008150\tIDA\tinvolved_in\tbiological_process\t123\tProcess\n"
OTHER = "10090\t2\tGO:0003674\tIEA\tenables\tmolecular_function\t-\tFunction\n"


class Gateway(HttpGateway):
    def __init__(self):
        self.dates = [DATE, DATE]
        self.download_date = DATE
        self.payload = gzip.compress(("\t".join(GENE2GO_HEADER) + "\n" + HUMAN + OTHER).encode())
        self.downloads = []

    def get_text(self, url, *, timeout):
        raise AssertionError("Version discovery must use HEAD only")

    def head(self, url, *, timeout):
        return HttpMetadata(url, {"Last-Modified": self.dates.pop(0)})

    def download(self, url, destination, *, timeout):
        self.downloads.append(url)
        destination.write_bytes(self.payload)
        return DownloadedResource(
            destination, HttpMetadata(url, {"Last-Modified": self.download_date})
        )


def source(gateway):
    return NcbiGene2GoSource(gateway, minimum_rows=2, minimum_human_rows=1)


def test_discovery_is_headers_only():
    gateway = Gateway()
    version = source(gateway).discover_latest(VersionProbeRequest())
    assert version.value == "2026-10-05"
    assert not gateway.downloads


def test_preserves_raw_gzip_all_taxa_and_duplicate_assertions(tmp_path):
    gateway = Gateway()
    gateway.payload = gzip.compress(("\t".join(GENE2GO_HEADER) + "\n" + HUMAN * 2 + OTHER).encode())
    snapshot = source(gateway).fetch(FetchRequest(tmp_path))
    assert snapshot.snapshot_id == "ncbi:gene2go:2026-10-05"
    assert snapshot.files[0].local_path.read_bytes() == gateway.payload
    assert snapshot.metadata["rows"] == 3
    assert snapshot.metadata["human_rows"] == 2
    assert snapshot.metadata["taxon_counts"] == {"9606": 2, "10090": 1}


@pytest.mark.parametrize(
    "failure",
    [
        "header",
        "columns",
        "go_id",
        "category",
        "truncated",
        "human",
        "download_date",
        "same_day_rebuild",
        "new_day",
    ],
)
def test_rejects_corrupt_incomplete_or_changed_export(tmp_path, failure):
    gateway = Gateway()
    text = gzip.decompress(gateway.payload).decode()
    if failure == "header":
        text = text.replace("#tax_id", "tax_id")
    elif failure == "columns":
        text += "9606\t1\n"
    elif failure == "go_id":
        text = text.replace("GO:0008150", "bad")
    elif failure == "category":
        text = text.replace("Process", "Unknown")
    elif failure == "human":
        text = text.replace("9606", "10090")
    elif failure == "download_date":
        gateway.download_date = "Mon, 05 Oct 2026 06:28:42 GMT"
    elif failure == "same_day_rebuild":
        gateway.dates[-1] = "Mon, 05 Oct 2026 06:28:42 GMT"
    elif failure == "new_day":
        gateway.dates[-1] = "Tue, 06 Oct 2026 05:28:42 GMT"
    gateway.payload = gzip.compress(text.encode())
    if failure == "truncated":
        gateway.payload = gateway.payload[:-8]
    with pytest.raises((SourceValidationError, VersionMismatchError)):
        source(gateway).fetch(FetchRequest(tmp_path))
    assert not (tmp_path / "ncbi" / "gene2go" / "2026-10-05").exists()


def test_rejects_unexpectedly_small_export(tmp_path):
    with pytest.raises(SourceValidationError, match="coverage"):
        NcbiGene2GoSource(Gateway()).fetch(FetchRequest(tmp_path))
