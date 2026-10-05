"""Acquisition contract: release coherence, byte fidelity and fail-closed validation."""

from __future__ import annotations

import gzip
from collections.abc import Mapping
from pathlib import Path

import pytest

from ifx_registry import FetchRequest, SourceValidationError, VersionProbeRequest
from ifx_registry.domain.errors import VersionMismatchError
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import (
    DownloadedResource,
    HttpGateway,
    HttpJson,
    HttpMetadata,
    HttpText,
)
from ifx_registry.infrastructure.sources.ensembl_ftp import (
    MINIMUM_ROWS,
    MYSQL_COLUMNS,
    XREF_HEADER,
    EnsemblHumanFtpSource,
    bsd_sum,
    ensembl_ftp_files,
    profile_file,
)


class Gateway(HttpGateway):
    def __init__(self, tmp_path: Path):
        self.payloads: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.release = 116
        self.probes = 0
        self.change_release = False
        self.change_checksums = False
        for spec in ensembl_ftp_files("116")[:9]:
            name = spec.name
            if name.endswith("gff3.gz"):
                content = (
                    "##gff-version 3\n#!genome-version GRCh38\n"
                    "1\te\tgene\t1\t2\t.\t+\t.\tID=gene:ENSG1\n"
                    "1\te\tmRNA\t1\t2\t.\t+\t.\ttranscript_id=ENST1\n"
                    "1\te\tCDS\t1\t2\t.\t+\t0\tprotein_id=ENSP1\n"
                )
            elif name.endswith("sql.gz"):
                content = "".join(f"CREATE TABLE `{t}` (\n);\n" for t in MYSQL_COLUMNS)
            elif "mysql" in name:
                kind = name.removeprefix("ensembl_mysql_").split(".")[0]
                content = "\t".join(["1"] * MYSQL_COLUMNS[kind]) + "\n"
            else:
                content = "\t".join(XREF_HEADER) + "\n"
                content += (
                    "\t".join(["ENSG1", "ENST1", "ENSP1", "X", "db", "info", "", "", ""]) + "\n"
                )
            self.payloads[spec.url] = gzip.compress(content.encode())
        for manifest in ensembl_ftp_files("116")[-3:]:
            lines = []
            for url, body in list(self.payloads.items()):
                if url.rsplit("/", 1)[0] == manifest.url.rsplit("/", 1)[0]:
                    path = tmp_path / "checksum-input"
                    path.write_bytes(body)
                    checksum, blocks = bsd_sum(path)
                    lines.append(f"{checksum} {blocks} {url.rsplit('/', 1)[1]}\n")
            self.payloads[manifest.url] = "".join(lines).encode()

    def get_json(
        self, url: str, *, timeout: float, headers: Mapping[str, str] | None = None
    ) -> HttpJson:
        self.probes += 1
        release = 117 if self.change_release and self.probes > 1 else self.release
        # The new release needs its own inventories before the final version guard.
        if release == 117:
            for key, value in list(self.payloads.items()):
                self.payloads[key.replace("116", "117")] = value.replace(b"116", b"117")
        return HttpJson({"releases": [release]}, HttpMetadata(url, {}))

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        value = self.payloads[url].decode()
        if self.change_checksums and self.probes > 1:
            value += "1 1 unrelated-file.gz\n"
        return HttpText(value, HttpMetadata(url, {}))

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        raise AssertionError("No HEAD needed")

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        self.calls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payloads[url])
        return DownloadedResource(destination, HttpMetadata(url, {}))


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Gateway:
    for kind in MINIMUM_ROWS:
        monkeypatch.setitem(MINIMUM_ROWS, kind, 1)
    return Gateway(tmp_path)


def test_complete_byte_faithful_bundle(tmp_path: Path, gateway: Gateway) -> None:
    source = EnsemblHumanFtpSource(gateway)
    snapshot = source.fetch(FetchRequest(tmp_path / "out", SourceVersion("116")))
    assert snapshot.snapshot_id == "ensembl:human_ftp:116"
    assert len(snapshot.files) == 12
    assert all(f.local_path.read_bytes() == gateway.payloads[f.source_url] for f in snapshot.files)
    assert all("ncbi" not in url for url in gateway.calls)
    assert any("chr_patch_hapl_scaff" in url for url in gateway.calls)
    assert snapshot.metadata["validation"]["publisher_checksums_verified"]


def test_wrong_pin_downloads_nothing(tmp_path: Path, gateway: Gateway) -> None:
    with pytest.raises(VersionMismatchError):
        EnsemblHumanFtpSource(gateway).fetch(FetchRequest(tmp_path / "out", SourceVersion("115")))
    assert gateway.calls == []


def test_missing_publisher_file_fails_discovery(gateway: Gateway) -> None:
    url = ensembl_ftp_files("116")[-3].url
    gateway.payloads[url] = b"1 1 unrelated.gz\n"
    with pytest.raises(SourceValidationError, match="Missing checksum"):
        EnsemblHumanFtpSource(gateway).discover_latest(VersionProbeRequest())


def test_corruption_never_commits(tmp_path: Path, gateway: Gateway) -> None:
    url = ensembl_ftp_files("116")[0].url
    gateway.payloads[url] += b"corrupt"
    with pytest.raises(SourceValidationError, match="checksum mismatch"):
        EnsemblHumanFtpSource(gateway).fetch(FetchRequest(tmp_path / "out"))
    assert not (tmp_path / "out/ensembl/human_ftp/116").exists()


@pytest.mark.parametrize("change", ["change_release", "change_checksums"])
def test_upstream_change_never_commits(tmp_path: Path, gateway: Gateway, change: str) -> None:
    setattr(gateway, change, True)
    with pytest.raises((VersionMismatchError, SourceValidationError)):
        EnsemblHumanFtpSource(gateway).fetch(FetchRequest(tmp_path / "out"))
    assert not (tmp_path / "out/ensembl/human_ftp/116").exists()


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("ensembl_entrez.tsv.gz", "wrong header\n"),
        ("ensembl_mysql_gene.txt.gz", "1\t2\n"),
        ("ensembl_mysql_schema.sql.gz", "CREATE TABLE `gene` (\n);\n"),
        ("ensembl_homo_sapiens.gff3.gz", "##gff-version 3\n#!genome-version GRCh37\n"),
    ],
)
def test_invalid_payload_shapes(tmp_path: Path, name: str, body: str) -> None:
    path = tmp_path / name
    path.write_bytes(gzip.compress(body.encode()))
    with pytest.raises(SourceValidationError):
        profile_file(path, name)


def test_bsd_checksum_known_value(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_bytes(b"")
    assert bsd_sum(path) == (0, 0)
    path.write_bytes(b"abc")
    assert bsd_sum(path) == (16556, 1)
