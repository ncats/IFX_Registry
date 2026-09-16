"""Contract tests for human-focused Babel compendium snapshots."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ifx_registry import FetchRequest, SourceValidationError, VersionProbeRequest
from ifx_registry.infrastructure.http import (
    DownloadedResource,
    HttpGateway,
    HttpMetadata,
    HttpText,
)
from ifx_registry.infrastructure.sources.babel import (
    BABEL_HUMAN_GENE,
    BabelHumanCompendiumSource,
)


class FakeBabelHttpGateway(HttpGateway):
    def __init__(self) -> None:
        self.text: dict[str, HttpText] = {}
        self.payloads: dict[str, bytes] = {}

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        del timeout
        return self.text[url]

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        raise AssertionError(f"Unexpected HEAD for {url}")

    def download(
        self,
        url: str,
        destination: Path,
        *,
        timeout: float,
    ) -> DownloadedResource:
        del timeout
        destination.write_bytes(self.payloads[url])
        return DownloadedResource(
            destination,
            HttpMetadata(
                url,
                {
                    "ETag": f'"{len(self.payloads[url]):x}"',
                    "Last-Modified": "Wed, 22 Jul 2026 16:29:14 GMT",
                },
            ),
        )


def _record(taxa: list[str], identifier: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "biolink:Gene",
                "identifiers": [{"i": identifier, "t": taxa}],
                "preferred_name": identifier,
                "taxa": taxa,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _gateway() -> tuple[FakeBabelHttpGateway, bytes]:
    root = "https://example.test/babel/"
    listing_url = f"{root}2026jul22/compendia/"
    gateway = FakeBabelHttpGateway()
    gateway.text[root] = HttpText(
        '<a href="2025nov4/">old</a><a href="2026jul22/">current</a>',
        HttpMetadata(root, {}),
    )
    false_positive = _record(["NCBITaxon:10090"], "NCBIGene:NCBITaxon:9606")
    human_one = _record(["NCBITaxon:9606"], "NCBIGene:1")
    human_two = _record(["NCBITaxon:9606"], "NCBIGene:2")
    gateway.payloads[f"{listing_url}Gene.txt.00"] = false_positive + human_one
    gateway.payloads[f"{listing_url}Gene.txt.01"] = human_two
    listing = "\n".join(
        [
            '<a href="Gene.txt">Gene.txt</a> 22-Jul-2026 16:01 999',
            (
                '<a href="Gene.txt.00">Gene.txt.00</a> 22-Jul-2026 16:29 '
                f'{len(gateway.payloads[f"{listing_url}Gene.txt.00"])}'
            ),
            (
                '<a href="Gene.txt.01">Gene.txt.01</a> 22-Jul-2026 16:29 '
                f'{len(gateway.payloads[f"{listing_url}Gene.txt.01"])}'
            ),
        ]
    )
    gateway.text[listing_url] = HttpText(listing, HttpMetadata(listing_url, {}))
    return gateway, human_one + human_two


def test_babel_source_discovers_latest_dated_release() -> None:
    gateway, _ = _gateway()
    source = BabelHumanCompendiumSource(
        gateway,
        replace(BABEL_HUMAN_GENE, minimum_human_records=2),
        root_url="https://example.test/babel/",
    )

    version = source.discover_latest(VersionProbeRequest())

    assert version.value == "2026jul22"
    assert version.version_date.isoformat() == "2026-07-22"
    assert version.evidence["method"] == "newest_versioned_babel_directory"


def test_babel_source_streams_chunks_and_keeps_only_semantic_human_taxa(
    tmp_path: Path,
) -> None:
    gateway, expected = _gateway()
    source = BabelHumanCompendiumSource(
        gateway,
        replace(BABEL_HUMAN_GENE, minimum_human_records=2),
        root_url="https://example.test/babel/",
    )

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "babel:human_gene_compendium:2026jul22"
    assert snapshot.files[0].relative_path.as_posix() == "nodenorm_genes.jsonl"
    assert snapshot.files[0].local_path.read_bytes() == expected
    assert snapshot.metadata["source_records"] == 3
    assert snapshot.metadata["human_records"] == 2
    assert [item["name"] for item in snapshot.metadata["files"]] == [
        "Gene.txt.00",
        "Gene.txt.01",
    ]
    assert all(item["sha256"] for item in snapshot.metadata["files"])
    assert not list(snapshot.files[0].local_path.parent.glob(".*.download"))


def test_babel_source_rejects_noncontiguous_chunks(tmp_path: Path) -> None:
    gateway, _ = _gateway()
    listing_url = "https://example.test/babel/2026jul22/compendia/"
    payload = gateway.payloads.pop(f"{listing_url}Gene.txt.01")
    gateway.payloads[f"{listing_url}Gene.txt.02"] = payload
    listing = gateway.text[listing_url].text.replace("Gene.txt.01", "Gene.txt.02")
    gateway.text[listing_url] = HttpText(listing, HttpMetadata(listing_url, {}))
    source = BabelHumanCompendiumSource(
        gateway,
        replace(BABEL_HUMAN_GENE, minimum_human_records=1),
        root_url="https://example.test/babel/",
    )

    with pytest.raises(SourceValidationError, match="not contiguous"):
        source.fetch(FetchRequest(tmp_path))


def test_babel_source_rejects_invalid_json_and_cleans_workspace(tmp_path: Path) -> None:
    gateway, _ = _gateway()
    listing_url = "https://example.test/babel/2026jul22/compendia/"
    bad_url = f"{listing_url}Gene.txt.00"
    old_size = len(gateway.payloads[bad_url])
    gateway.payloads[bad_url] = b"not-json\n"
    listing = gateway.text[listing_url].text
    listing = listing.replace(
        str(old_size),
        str(len(gateway.payloads[bad_url])),
        1,
    )
    gateway.text[listing_url] = HttpText(listing, HttpMetadata(listing_url, {}))
    source = BabelHumanCompendiumSource(
        gateway,
        replace(BABEL_HUMAN_GENE, minimum_human_records=1),
        root_url="https://example.test/babel/",
    )

    with pytest.raises(SourceValidationError, match="not valid JSON"):
        source.fetch(FetchRequest(tmp_path))

    assert not (tmp_path / "babel" / "human_gene_compendium" / "2026jul22").exists()
