"""Contract tests for the independent UniProt SPARQL acquisition sources."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from ifx_registry import FetchRequest, SourceValidationError, SourceVersion
from ifx_registry.domain.errors import VersionMismatchError
from ifx_registry.infrastructure.http import DownloadedResource, HttpMetadata
from ifx_registry.infrastructure.sources.uniprot_sparql_exports import (
    ENSEMBL,
    UNIREF,
    UniProtSparqlExportSource,
)
from tests.unit.infrastructure.test_uniprot_isoforms_source import FakeSparqlGateway, _binding


class ExportGateway(FakeSparqlGateway):
    def __init__(self, definition=UNIREF, **kwargs):
        super().__init__(**kwargs)
        self.definition = definition
        self.count = 1
        self.payload = {
            "results": {
                "bindings": [
                    _binding(
                        protein="http://purl.uniprot.org/uniprot/P04637-2",
                        cluster="http://purl.uniprot.org/uniref/UniRef100_P04637",
                        identity="1.0",
                        isSeed="false",
                        isRepresentative="true",
                    )
                    if definition == UNIREF
                    else _binding(
                        ensemblTranscript="http://rdf.ebi.ac.uk/resource/ensembl.transcript/ENST000001.2",
                        isoform="http://purl.uniprot.org/isoforms/P04637-2",
                    )
                ]
            }
        }

    def download(self, url, destination, *, timeout):
        query = parse_qs(urlsplit(url).query)["query"][0]
        self.query_calls.append(query)
        payload = (
            {"results": {"bindings": [_binding(count=str(self.count))]}}
            if "COUNT(*)" in query
            else self.payload
        )
        destination.write_text(json.dumps(payload))
        return DownloadedResource(
            destination, HttpMetadata(url, {"content-type": "application/json"})
        )


def source(gateway):
    return UniProtSparqlExportSource(gateway, replace(gateway.definition, minimum_rows=1))


@pytest.mark.parametrize("definition", [UNIREF, ENSEMBL])
def test_full_export_preserves_raw_results_and_consumer_columns(tmp_path, definition):
    snapshot = source(ExportGateway(definition)).fetch(FetchRequest(tmp_path))
    assert snapshot.snapshot_id == f"uniprot:{definition.dataset}:2026_03-export1"
    assert {str(file.relative_path) for file in snapshot.files} == {
        "response.json",
        "count.json",
        "query.rq",
        definition.filename,
    }
    path = next(
        file.local_path for file in snapshot.files if file.relative_path.name == definition.filename
    )
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    assert tuple(rows[0]) == definition.columns
    if definition == UNIREF:
        assert rows[0]["uniprot_id"] == "P04637-2"
        assert rows[0]["uniref100_is_seed"] == "False"
        assert rows[0]["uniref100_is_representative"] == "True"
    else:
        assert rows[0]["ensembl_transcript_id_version"] == "ENST000001.2"
    assert snapshot.metadata["rows"] == snapshot.metadata["upstream_count"] == 1


@pytest.mark.parametrize(
    "failure",
    ["truncated", "duplicate", "malformed", "empty", "wrong_identity", "wrong_uri", "wrong_flag"],
)
def test_invalid_results_never_commit(tmp_path, failure):
    gateway = ExportGateway()
    rows = gateway.payload["results"]["bindings"]
    if failure == "truncated":
        gateway.count = 2
    elif failure == "duplicate":
        rows.append(rows[0])
        gateway.count = 2
    elif failure == "malformed":
        gateway.payload = {"results": {}}
    elif failure == "empty":
        rows.clear()
        gateway.count = 0
    else:
        field, value = {
            "wrong_identity": ("identity", "0.9"),
            "wrong_uri": ("protein", "http://example.org/P04637"),
            "wrong_flag": ("isSeed", "perhaps"),
        }[failure]
        rows[0][field]["value"] = value
    with pytest.raises(SourceValidationError):
        source(gateway).fetch(FetchRequest(tmp_path))
    assert not list(tmp_path.rglob("2026_03-export1"))
    assert not list(tmp_path.rglob("*.part"))


def test_wrong_pin_stops_before_data_acquisition(tmp_path):
    gateway = ExportGateway()
    with pytest.raises(VersionMismatchError):
        source(gateway).fetch(
            FetchRequest(tmp_path, expected_version=SourceVersion("2026_02-export1"))
        )
    assert all("pav/version" in query for query in gateway.query_calls)


def test_release_change_during_export_never_commits(tmp_path):
    gateway = ExportGateway(releases=["2026_03", "2026_04"])
    with pytest.raises(SourceValidationError, match="do not agree"):
        source(gateway).fetch(FetchRequest(tmp_path))
    assert not list(tmp_path.rglob("2026_03-export1"))


def test_query_flags_are_bound_to_matching_member_and_protein():
    assert "?member up:sequenceFor ?protein" in UNIREF.query
    assert "?member up:seedFor ?cluster" in UNIREF.query
    assert "?protein up:representativeFor ?cluster" in UNIREF.query
    assert "up:organism taxon:9606" in UNIREF.query
    assert "up:organism taxon:9606" in ENSEMBL.query
    assert "?ensemblTranscript rdfs:seeAlso ?isoform" in ENSEMBL.query
    assert "?protein up:sequence ?isoform" not in ENSEMBL.query


def test_retries_acquisition_with_bounded_backoff(tmp_path):
    from ifx_registry.domain.errors import SourceAcquisitionError

    class FlakyGateway(ExportGateway):
        failures = 2

        def download(self, url, destination, *, timeout):
            assert timeout == 300
            if self.failures:
                self.failures -= 1
                raise SourceAcquisitionError("temporary endpoint failure")
            return super().download(url, destination, timeout=timeout)

    delays = []
    gateway = FlakyGateway()
    adapter = UniProtSparqlExportSource(
        gateway,
        replace(UNIREF, minimum_rows=1),
        sleeper=delays.append,
    )
    adapter.fetch(FetchRequest(tmp_path))
    assert delays == [2, 4]


def test_failed_retries_do_not_commit(tmp_path):
    from ifx_registry.domain.errors import SourceAcquisitionError

    class FailedGateway(ExportGateway):
        attempts = 0

        def download(self, url, destination, *, timeout):
            self.attempts += 1
            raise SourceAcquisitionError("endpoint unavailable")

    gateway = FailedGateway()
    adapter = UniProtSparqlExportSource(
        gateway,
        UNIREF,
        sleeper=lambda _: None,
    )
    with pytest.raises(SourceAcquisitionError):
        adapter.fetch(FetchRequest(tmp_path))
    assert gateway.attempts == 3
    assert not list(tmp_path.rglob("2026_03-export1"))


def test_truncated_json_never_commits(tmp_path):
    class TruncatedGateway(ExportGateway):
        def download(self, url, destination, *, timeout):
            result = super().download(url, destination, timeout=timeout)
            if destination.name == "response.json":
                destination.write_text('{"results":{"bindings":[')
            return result

    with pytest.raises(SourceValidationError, match="invalid JSON"):
        source(TruncatedGateway()).fetch(FetchRequest(tmp_path))
    assert not list(tmp_path.rglob("2026_03-export1"))
