from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from ifx_registry import FetchRequest, SourceValidationError, VersionProbeRequest
from ifx_registry.domain.errors import SourceAcquisitionError
from ifx_registry.infrastructure.http import (
    DownloadedResource,
    HttpGateway,
    HttpJson,
    HttpMetadata,
    HttpText,
)
from ifx_registry.infrastructure.sources.uniprot import UNIPROT_RELEASE_PROBE_URL
from ifx_registry.infrastructure.sources.uniprot_isoforms import (
    UNIPROT_CANONICAL_ISOFORMS_FILE,
    UNIPROT_COMPUTATIONAL_ISOFORMS_FILE,
    UNIPROT_ISOFORM_COLUMNS,
    UniProtHumanIsoformsSource,
)


def _binding(**values: str) -> dict[str, dict[str, str]]:
    return {name: {"type": "uri", "value": value} for name, value in values.items()}


class FakeSparqlGateway(HttpGateway):
    def __init__(
        self,
        releases: list[str] | None = None,
        *,
        empty_computational: bool = False,
        computational_exists: bool = False,
    ) -> None:
        self.releases = list(releases or ["2026_03", "2026_03"])
        self.query_calls: list[str] = []
        self.failures_remaining = 0
        self.empty_computational = empty_computational
        self.computational_exists = computational_exists

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        raise AssertionError((url, timeout))

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        assert url == UNIPROT_RELEASE_PROBE_URL
        return HttpMetadata(
            url,
            {
                "X-UniProt-Release": "2026_03",
                "X-UniProt-Release-Date": "02-September-2026",
            },
        )

    def download(
        self, url: str, destination: Path, *, timeout: float
    ) -> DownloadedResource:
        raise AssertionError((url, destination, timeout))

    def get_json(
        self,
        url: str,
        *,
        timeout: float,
        headers=None,
    ) -> HttpJson:
        del timeout
        assert headers == {"Accept": "application/sparql-results+json"}
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise SourceAcquisitionError("temporary failure")
        query = parse_qs(urlsplit(url).query)["query"][0]
        self.query_calls.append(query)
        if "pav/version" in query:
            release = self.releases.pop(0)
            bindings = [_binding(version=release)]
            payload = {"results": {"bindings": bindings}}
        elif "ASK {" in query:
            payload = {"boolean": self.computational_exists}
        elif "potentialSequence" in query:
            bindings = (
                []
                if self.empty_computational
                else [
                    _binding(
                        entry="http://purl.uniprot.org/uniprot/P00002",
                        sequence="http://purl.uniprot.org/isoforms/P00002-3",
                        isCanonical="false",
                    )
                ]
            )
            payload = {"results": {"bindings": bindings}}
        else:
            bindings = [
                _binding(
                    entry="http://purl.uniprot.org/uniprot/P00001",
                    sequence="http://purl.uniprot.org/isoforms/P00001-1",
                    sequenceValue="MPEPTIDE",
                    isCanonical="true",
                )
            ]
            payload = {"results": {"bindings": bindings}}
        return HttpJson(
            payload,
            HttpMetadata(url, {"content-type": "application/sparql-results+json"}),
        )


def test_discovers_matching_sparql_and_rest_release() -> None:
    source = UniProtHumanIsoformsSource(FakeSparqlGateway())

    version = source.discover_latest(VersionProbeRequest())

    assert version.value == "2026_03-export1"
    assert version.version_date.isoformat() == "2026-09-02"
    assert version.evidence["sparql_release"] == "2026_03"


def test_generates_both_established_isoform_files(tmp_path: Path) -> None:
    source = UniProtHumanIsoformsSource(
        FakeSparqlGateway(),
        sleeper=lambda _: None,
        minimum_canonical_rows=1,
        minimum_computational_rows=1,
    )

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "uniprot:human_isoforms:2026_03-export1"
    assert [str(item.relative_path) for item in snapshot.files] == [
        UNIPROT_CANONICAL_ISOFORMS_FILE,
        UNIPROT_COMPUTATIONAL_ISOFORMS_FILE,
    ]
    with snapshot.files[0].local_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert tuple(rows[0]) == UNIPROT_ISOFORM_COLUMNS
    assert rows == [
        {
            "entry": "P00001",
            "uniprot_id": "P00001-1",
            "isoform": "P00001-1",
            "uniprot_sequence": "MPEPTIDE",
            "isCanonical": "true",
        }
    ]
    with snapshot.files[1].local_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["uniprot_sequence"] == ""
    assert snapshot.metadata["exports"][UNIPROT_CANONICAL_ISOFORMS_FILE]["rows"] == 1
    assert snapshot.metadata["release_confirmed_after_export"] == "2026_03-export1"


def test_rejects_sparql_release_that_disagrees_with_rest() -> None:
    source = UniProtHumanIsoformsSource(FakeSparqlGateway(["2026_02"]))

    with pytest.raises(SourceValidationError, match="do not agree"):
        source.discover_latest(VersionProbeRequest())


def test_retries_transient_sparql_failure() -> None:
    gateway = FakeSparqlGateway(["2026_03"])
    gateway.failures_remaining = 1
    delays: list[float] = []
    source = UniProtHumanIsoformsSource(gateway, sleeper=delays.append)

    version = source.discover_latest(VersionProbeRequest())

    assert version.value == "2026_03-export1"
    assert delays == [2.0]


def test_rejects_implausibly_small_isoform_export(tmp_path: Path) -> None:
    source = UniProtHumanIsoformsSource(
        FakeSparqlGateway(),
        sleeper=lambda _: None,
        minimum_canonical_rows=2,
        minimum_computational_rows=1,
    )

    with pytest.raises(SourceValidationError, match="expected at least 2"):
        source.fetch(FetchRequest(tmp_path))


def test_accepts_empty_computational_export_only_after_independent_ask(
    tmp_path: Path,
) -> None:
    gateway = FakeSparqlGateway(empty_computational=True)
    source = UniProtHumanIsoformsSource(
        gateway,
        sleeper=lambda _: None,
        minimum_canonical_rows=1,
        minimum_computational_rows=1_000,
    )

    snapshot = source.fetch(FetchRequest(tmp_path))

    computational = snapshot.files[1].local_path
    with computational.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []
    profile = snapshot.metadata["exports"][UNIPROT_COMPUTATIONAL_ISOFORMS_FILE]
    assert profile["rows"] == 0
    assert profile["empty_confirmed_by_independent_ask"] is True
    assert any("ASK {" in query for query in gateway.query_calls)


def test_rejects_empty_computational_export_when_ask_finds_records(
    tmp_path: Path,
) -> None:
    gateway = FakeSparqlGateway(
        empty_computational=True,
        computational_exists=True,
    )
    source = UniProtHumanIsoformsSource(
        gateway,
        sleeper=lambda _: None,
        minimum_canonical_rows=1,
        minimum_computational_rows=1_000,
    )

    with pytest.raises(SourceValidationError, match="existence query found"):
        source.fetch(FetchRequest(tmp_path))
