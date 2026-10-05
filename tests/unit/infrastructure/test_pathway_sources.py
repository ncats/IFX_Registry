from __future__ import annotations

import json

import pytest

from ifx_registry import FetchRequest, SourceVersion, VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError, VersionMismatchError
from ifx_registry.infrastructure.http import DownloadedResource, HttpGateway, HttpMetadata, HttpText
from ifx_registry.infrastructure.sources.panther_pathways import (
    HMM_INDEX,
    PATHWAY_INDEX,
    PantherPathwaysSource,
)
from ifx_registry.infrastructure.sources.reactome import (
    REACTOME_FILES,
    REACTOME_VERSION_URL,
    ReactomePathwaysSource,
)
from ifx_registry.infrastructure.sources.wikipathways_list import (
    PATHWAY_LIST_URL,
    WikiPathwaysListSource,
)

MODIFIED = "Mon, 05 Oct 2026 06:27:02 GMT"


class Gateway(HttpGateway):
    def __init__(self):
        self.texts = {}
        self.payloads = {}
        self.downloads = []
        self.on_download = lambda: None
        self.modified = MODIFIED

    def get_text(self, url, *, timeout):
        return HttpText(self.texts[url], HttpMetadata(url, {"Last-Modified": self.modified}))

    def head(self, url, *, timeout):
        return HttpMetadata(url, {"Last-Modified": self.modified})

    def download(self, url, destination, *, timeout):
        self.downloads.append(url)
        destination.write_bytes(self.payloads[url])
        metadata = HttpMetadata(url, {"Last-Modified": self.modified})
        self.on_download()
        return DownloadedResource(destination, metadata)


def panther_gateway():
    gateway = Gateway()
    gateway.texts = {
        PATHWAY_INDEX: 'href="SequenceAssociationPathway3.6.8.txt"',
        HMM_INDEX: 'href="PANTHER19.0_HMM_classifications"',
    }
    gateway.payloads = {
        PATHWAY_INDEX + "SequenceAssociationPathway3.6.8.txt": (
            b"P12345\tPathway\tP11111\tGene\tHUMAN|HGNC=1|UniProtKB=P12345\tProtein\tISS\t\t\tPTHR12345\tFamily\n"
        ),
        HMM_INDEX + "PANTHER19.0_HMM_classifications": b"PTHR12345\tFamily\t\t\t\t\t\n",
    }
    return gateway


def wiki_gateway():
    gateway = Gateway()
    text = json.dumps(
        {
            "organisms": [
                {
                    "latin": "Homo_sapiens",
                    "pathways": [{"id": "WP1", "name": "Example", "species": "Homo sapiens"}],
                }
            ]
        }
    )
    gateway.texts[PATHWAY_LIST_URL] = text
    gateway.payloads[PATHWAY_LIST_URL] = text.encode()
    return gateway


def reactome_gateway():
    gateway = Gateway()
    gateway.texts[REACTOME_VERSION_URL] = "97"
    gateway.payloads = {spec.url: b"original member\n" for spec in REACTOME_FILES}
    gateway.payloads[REACTOME_FILES[0].url] = b"R-HSA-123\tPathway\tHomo sapiens\n"
    gateway.payloads[REACTOME_FILES[1].url] = (
        b"1\tR-HSA-123\thttps://reactome.org/PathwayBrowser/#/R-HSA-123\t"
        b"Pathway\tTAS\tHomo sapiens\n"
    )
    return gateway


def test_panther_preserves_two_original_files_and_both_release_identities(tmp_path):
    gateway = panther_gateway()
    snapshot = PantherPathwaysSource(gateway).fetch(FetchRequest(tmp_path))
    assert snapshot.snapshot_id == "panther:pathways:19.0-pathway3.6.8"
    assert snapshot.version.evidence["hmm_release"] == "19.0"
    assert snapshot.version.evidence["pathway_release"] == "3.6.8"
    for file in snapshot.files:
        assert file.local_path.read_bytes() == gateway.payloads[file.source_url]
    assert (
        snapshot.metadata["file_counts"]["SequenceAssociationPathway3.6.8.txt"]["human_rows"] == 1
    )


@pytest.mark.parametrize("failure", ["columns", "human", "date", "release"])
def test_panther_rejects_invalid_or_moving_source(tmp_path, failure):
    gateway = panther_gateway()
    url = PATHWAY_INDEX + "SequenceAssociationPathway3.6.8.txt"
    if failure == "columns":
        gateway.payloads[url] = b"P12345\tbad\n"
    elif failure == "human":
        gateway.payloads[url] = gateway.payloads[url].replace(b"HUMAN|", b"MOUSE|")
    elif failure == "date":
        gateway.on_download = lambda: setattr(gateway, "modified", "Tue, 06 Oct 2026 06:27:02 GMT")
    else:
        gateway.on_download = lambda: gateway.texts.update(
            {HMM_INDEX: 'href="PANTHER20.0_HMM_classifications"'}
        )
    with pytest.raises((SourceValidationError, VersionMismatchError)):
        PantherPathwaysSource(gateway).fetch(FetchRequest(tmp_path))
    assert not (tmp_path / "panther" / "pathways" / "19.0-pathway3.6.8").exists()


def test_wiki_versions_content_changes_even_with_same_http_date(tmp_path):
    gateway = wiki_gateway()
    source = WikiPathwaysListSource(gateway)
    first = source.discover_latest(VersionProbeRequest())
    gateway.texts[PATHWAY_LIST_URL] = gateway.texts[PATHWAY_LIST_URL].replace("Example", "Updated")
    second = source.discover_latest(VersionProbeRequest())
    assert first.value != second.value
    assert first.version_date == second.version_date
    gateway.payloads[PATHWAY_LIST_URL] = gateway.texts[PATHWAY_LIST_URL].encode()
    snapshot = source.fetch(FetchRequest(tmp_path, expected_version=second))
    assert snapshot.metadata["human_pathways"] == 1
    assert "not a monthly GMT" in snapshot.metadata["release_relationship"]
    assert snapshot.files[0].local_path.read_bytes() == gateway.payloads[PATHWAY_LIST_URL]


@pytest.mark.parametrize(
    "failure", ["changed_download", "changed_confirmation", "html", "duplicate", "no_human"]
)
def test_wiki_rejects_changed_or_invalid_json(tmp_path, failure):
    gateway = wiki_gateway()
    if failure == "changed_download":
        gateway.payloads[PATHWAY_LIST_URL] = gateway.payloads[PATHWAY_LIST_URL].replace(
            b"Example", b"Changed"
        )
    elif failure == "changed_confirmation":
        gateway.on_download = lambda: gateway.texts.update(
            {PATHWAY_LIST_URL: gateway.texts[PATHWAY_LIST_URL].replace("Example", "Changed")}
        )
    elif failure == "html":
        gateway.texts[PATHWAY_LIST_URL] = "<html>Error</html>"
    elif failure == "no_human":
        gateway.texts[PATHWAY_LIST_URL] = gateway.texts[PATHWAY_LIST_URL].replace(
            "Homo sapiens", "Mus musculus"
        )
    else:
        data = json.loads(gateway.texts[PATHWAY_LIST_URL])
        records = data["organisms"][0]["pathways"]
        records.append(records[0])
        gateway.texts[PATHWAY_LIST_URL] = json.dumps(data)
    with pytest.raises((SourceValidationError, VersionMismatchError)):
        WikiPathwaysListSource(gateway).fetch(FetchRequest(tmp_path))
    assert not list(tmp_path.rglob("listPathways.json"))


def test_reactome_expanded_bundle_cannot_be_fetched_as_legacy_pin(tmp_path):
    gateway = reactome_gateway()
    with pytest.raises(VersionMismatchError):
        ReactomePathwaysSource(gateway).fetch(
            FetchRequest(tmp_path, expected_version=SourceVersion("97"))
        )
    assert not gateway.downloads


@pytest.mark.parametrize("index", [0, 1])
def test_reactome_rejects_bad_added_files(tmp_path, index):
    gateway = reactome_gateway()
    gateway.payloads[REACTOME_FILES[index].url] = b"<html>upstream error</html>\n"
    with pytest.raises(SourceValidationError):
        ReactomePathwaysSource(gateway).fetch(FetchRequest(tmp_path))
    assert not (tmp_path / "reactome" / "pathways" / "97-bundle1").exists()


def test_reactome_preserves_upstream_accession_strings_in_ncbi_column(tmp_path):
    gateway = reactome_gateway()
    url = REACTOME_FILES[1].url
    gateway.payloads[url] = gateway.payloads[url].replace(b"1\t", b"J02428.1\t", 1)
    snapshot = ReactomePathwaysSource(gateway).fetch(FetchRequest(tmp_path))
    counts = snapshot.metadata["added_file_counts"]["NCBI2Reactome_All_Levels.txt"]
    assert counts["non_numeric_source_ids"] == 1
    member = next(f for f in snapshot.files if str(f.relative_path) == REACTOME_FILES[1].name)
    assert member.local_path.read_bytes() == gateway.payloads[url]
