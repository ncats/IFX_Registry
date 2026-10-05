from __future__ import annotations

import json

import pytest

from ifx_registry import FetchRequest, SourceVersion, VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError, VersionMismatchError
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway, HttpMetadata, HttpText
from ifx_registry.infrastructure.sources.go import GO_ROOTS, GoOntologySource


class GoGateway(HttpGateway):
    def __init__(self):
        self.releases = ["2026-07-26", "2026-07-26"]
        self.download_release = "2026-07-26"
        self.json_release = "2026-07-26"
        self.terms = sorted(GO_ROOTS)
        self.downloads = []

    def get_text(self, url, *, timeout):
        return HttpText(
            f"format-version: 1.2\ndata-version: releases/{self.releases.pop(0)}\n",
            HttpMetadata(url, {}),
        )

    def head(self, url, *, timeout):
        raise AssertionError("GO must not use file timestamps for its ontology version")

    def download(self, url, destination, *, timeout):
        self.downloads.append(url)
        if destination.name == "go.obo":
            destination.write_text(
                f"format-version: 1.2\ndata-version: releases/{self.download_release}\n"
                + "".join(f"\n[Term]\nid: {t}\nname: test\n" for t in self.terms)
            )
        else:
            destination.write_text(
                json.dumps(
                    {
                        "graphs": [
                            {
                                "meta": {
                                    "version": f"http://purl.obolibrary.org/obo/go/releases/{self.json_release}/go.owl"
                                },
                                "nodes": [
                                    {
                                        "id": "http://purl.obolibrary.org/obo/"
                                        + t.replace(":", "_"),
                                        "type": "CLASS",
                                    }
                                    for t in self.terms
                                ],
                                "edges": [{"sub": "a", "pred": "is_a", "obj": "b"}],
                            }
                        ]
                    }
                )
            )
        return DownloadedResource(destination, HttpMetadata(url, {}))


def test_go_bundle_retains_json_and_adds_obo_with_distinct_identity(tmp_path):
    gateway = GoGateway()
    source = GoOntologySource(gateway, minimum_terms=3)
    result = source.fetch(FetchRequest(tmp_path))
    assert result.snapshot_id == "go:ontology:2026-07-26-bundle1"
    assert result.version.version_date.isoformat() == "2026-07-26"
    assert {str(f.relative_path) for f in result.files} == {"go-basic.json", "go.obo"}
    assert result.metadata["obo_data_version"] == "releases/2026-07-26"
    assert result.metadata["obo_terms"] == result.metadata["json_terms"] == 3


@pytest.mark.parametrize(
    "failure", ["obo_release", "json_release", "roots", "small", "duplicate", "rollover"]
)
def test_go_rejects_inconsistent_or_incomplete_bundle(tmp_path, failure):
    gateway = GoGateway()
    minimum = 3
    if failure == "obo_release":
        gateway.download_release = "2026-08-01"
    elif failure == "json_release":
        gateway.json_release = "2026-08-01"
    elif failure == "roots":
        gateway.terms = ["GO:1234567", "GO:1234568", "GO:1234569"]
    elif failure == "small":
        minimum = 4
    elif failure == "duplicate":
        gateway.terms.append(gateway.terms[0])
    else:
        gateway.releases[-1] = "2026-08-01"
    with pytest.raises((SourceValidationError, VersionMismatchError)):
        GoOntologySource(gateway, minimum_terms=minimum).fetch(FetchRequest(tmp_path))
    assert not list(tmp_path.rglob("2026-07-26-bundle1"))
    assert not list(tmp_path.rglob("*.part"))


def test_json_only_pin_cannot_fetch_expanded_bundle(tmp_path):
    gateway = GoGateway()
    with pytest.raises(VersionMismatchError):
        GoOntologySource(gateway).fetch(
            FetchRequest(
                tmp_path,
                expected_version=SourceVersion("2026-08-08"),
            )
        )
    assert not gateway.downloads


@pytest.mark.parametrize("release", ["not-a-date", "2026-99-99"])
def test_bad_obo_version_rejected(release):
    gateway = GoGateway()
    gateway.releases = [release]
    with pytest.raises(SourceValidationError):
        GoOntologySource(gateway).discover_latest(VersionProbeRequest())
