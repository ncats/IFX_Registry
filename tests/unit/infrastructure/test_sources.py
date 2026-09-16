"""Contract-focused tests for built-in source adapters."""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from ifx_registry import FetchRequest, FetchSource, SourceValidationError, VersionProbeRequest
from ifx_registry.domain.errors import VersionMismatchError
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import (
    DownloadedResource,
    HttpGateway,
    HttpJson,
    HttpMetadata,
    HttpText,
)
from ifx_registry.infrastructure.sources.ensembl import (
    ENSEMBL_ARCHIVE_URL,
    ENSEMBL_BIOMART_ENDPOINTS,
    ENSEMBL_EXPORTS,
    ENSEMBL_RELEASE_URL,
    BioMartExport,
    EnsemblHumanBioMartSource,
    archive_biomart_endpoint,
    biomart_url,
)
from ifx_registry.infrastructure.sources.hgnc import (
    HGNC_COMPLETE_SET_FILE,
    HGNC_COMPLETE_SET_HEADER,
    HGNC_COMPLETE_SET_URL,
    HgncCompleteSetSource,
)
from ifx_registry.infrastructure.sources.inspected_files import (
    INSPECTED_FILE_SOURCES,
    InspectedFileSource,
)
from ifx_registry.infrastructure.sources.last_modified import (
    LAST_MODIFIED_SOURCES,
    LastModifiedHttpSource,
)
from ifx_registry.infrastructure.sources.ncbi import (
    NCBI_HUMAN_GENE_INFO_FILE,
    NCBI_HUMAN_GENE_INFO_HEADER,
    NCBI_HUMAN_GENE_INFO_URL,
    NcbiHumanGeneInfoSource,
)
from ifx_registry.infrastructure.sources.ncbi_gene_mappings import (
    NCBI_GENE_MAPPING_FILES,
    NcbiGeneIdentifierMappingsSource,
)
from ifx_registry.infrastructure.sources.reactome import (
    REACTOME_FILES,
    REACTOME_VERSION_URL,
    ReactomePathwaysSource,
)
from ifx_registry.infrastructure.sources.uniprot import (
    UNIPROT_HUMAN_IDMAPPING_NAME,
    UNIPROT_HUMAN_IDMAPPING_URL,
    UNIPROT_HUMAN_REFERENCE_PROTEOME_NAME,
    UNIPROT_HUMAN_REFERENCE_PROTEOME_URL,
    UNIPROT_HUMAN_URL,
    UNIPROT_RELEASE_PROBE_URL,
    UNIPROT_REVIEWED_HUMAN_URL,
    UniProtHumanIdMappingSource,
    UniProtHumanReferenceProteomeSource,
    UniProtHumanSource,
    normalize_uniprot_release_date,
    validate_reviewed_in_full,
)


class FakeHttpGateway(HttpGateway):
    def __init__(self) -> None:
        self.text_responses: dict[str, HttpText] = {}
        self.head_responses: dict[str, HttpMetadata] = {}
        self.json_responses: dict[str, HttpJson] = {}
        self.payloads: dict[str, bytes] = {}
        self.download_headers: dict[str, dict[str, str]] = {}
        self.download_calls: list[tuple[str, Path, float]] = []

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        return self.text_responses[url]

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        return self.head_responses[url]

    def get_json(
        self,
        url: str,
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
    ) -> HttpJson:
        del timeout, headers
        return self.json_responses[url]

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        self.download_calls.append((url, destination, timeout))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payloads[url])
        return DownloadedResource(
            path=destination,
            metadata=HttpMetadata(
                final_url=url,
                headers=self.download_headers.get(
                    url,
                    {"content-type": "application/octet-stream"},
                ),
            ),
        )


def _gzip_uniprot(records: list[dict[str, object]]) -> bytes:
    return gzip.compress(json.dumps({"results": records}).encode())


def _reactome_gateway(
    *,
    last_modified: str | None = "Sat, 20 Jun 2026 00:20:37 GMT",
) -> FakeHttpGateway:
    gateway = FakeHttpGateway()
    gateway.text_responses[REACTOME_VERSION_URL] = HttpText(
        text="97\n",
        metadata=HttpMetadata(REACTOME_VERSION_URL, {"content-type": "text/plain"}),
    )
    for file_spec in REACTOME_FILES:
        gateway.payloads[file_spec.url] = f"contents of {file_spec.name}\n".encode()
        gateway.download_headers[file_spec.url] = {"content-type": "text/plain"}
    if last_modified:
        gateway.download_headers[REACTOME_FILES[-1].url]["Last-Modified"] = last_modified
    return gateway


def _uniprot_gateway(
    *,
    full_records: list[dict[str, object]],
    reviewed_records: list[dict[str, object]],
) -> FakeHttpGateway:
    gateway = FakeHttpGateway()
    gateway.head_responses[UNIPROT_RELEASE_PROBE_URL] = HttpMetadata(
        final_url=UNIPROT_RELEASE_PROBE_URL,
        headers={
            "X-UniProt-Release": "2026_03",
            "X-UniProt-Release-Date": "02-September-2026",
        },
    )
    gateway.payloads[UNIPROT_HUMAN_URL] = _gzip_uniprot(full_records)
    gateway.payloads[UNIPROT_REVIEWED_HUMAN_URL] = _gzip_uniprot(reviewed_records)
    gateway.download_headers[UNIPROT_HUMAN_URL] = {"content-type": "application/x-gzip"}
    gateway.download_headers[UNIPROT_REVIEWED_HUMAN_URL] = {"content-type": "application/x-gzip"}
    return gateway


def _small_ensembl_exports() -> tuple[BioMartExport, ...]:
    return tuple(
        BioMartExport(
            export.name,
            export.query,
            export.expected_header,
            minimum_rows=1,
            delimiter=export.delimiter,
        )
        for export in ENSEMBL_EXPORTS
    )


def _ensembl_gateway(exports: tuple[BioMartExport, ...]) -> FakeHttpGateway:
    gateway = FakeHttpGateway()
    gateway.json_responses[ENSEMBL_RELEASE_URL] = HttpJson(
        {"releases": [115, 116]},
        HttpMetadata(ENSEMBL_RELEASE_URL, {"content-type": "application/json"}),
    )
    gateway.text_responses[ENSEMBL_ARCHIVE_URL] = HttpText(
        "<tr><td>Ensembl 116</td><td>Jun 2026</td></tr>",
        HttpMetadata(ENSEMBL_ARCHIVE_URL, {"content-type": "text/html"}),
    )
    endpoint = archive_biomart_endpoint(date(2026, 6, 1))
    for export in exports:
        url = biomart_url(endpoint, export.query)
        row = [f"value-{index}" for index in range(len(export.expected_header))]
        payload = "\t".join(export.expected_header) + "\n" + "\t".join(row) + "\n[success]\n"
        gateway.payloads[url] = payload.encode()
    return gateway


def _ncbi_gene_info_gateway(*rows: tuple[str, ...]) -> FakeHttpGateway:
    gateway = FakeHttpGateway()
    gateway.head_responses[NCBI_HUMAN_GENE_INFO_URL] = HttpMetadata(
        NCBI_HUMAN_GENE_INFO_URL,
        {"Last-Modified": "Tue, 15 Sep 2026 07:17:42 GMT"},
    )
    text = "\t".join(NCBI_HUMAN_GENE_INFO_HEADER) + "\n"
    text += "".join("\t".join(row) + "\n" for row in rows)
    gateway.payloads[NCBI_HUMAN_GENE_INFO_URL] = gzip.compress(text.encode())
    gateway.download_headers[NCBI_HUMAN_GENE_INFO_URL] = {
        "content-type": "application/x-gzip"
    }
    return gateway


def _hgnc_gateway(*rows: tuple[str, ...]) -> FakeHttpGateway:
    gateway = FakeHttpGateway()
    gateway.head_responses[HGNC_COMPLETE_SET_URL] = HttpMetadata(
        HGNC_COMPLETE_SET_URL,
        {"Last-Modified": "Tue, 15 Sep 2026 13:48:09 GMT"},
    )
    text = "\t".join(HGNC_COMPLETE_SET_HEADER) + "\n"
    text += "".join("\t".join(row) + "\n" for row in rows)
    gateway.payloads[HGNC_COMPLETE_SET_URL] = text.encode()
    gateway.download_headers[HGNC_COMPLETE_SET_URL] = {
        "content-type": "text/plain;charset=utf-8"
    }
    return gateway


def _hgnc_row(hgnc_id: str = "HGNC:5") -> tuple[str, ...]:
    row = [""] * len(HGNC_COMPLETE_SET_HEADER)
    row[HGNC_COMPLETE_SET_HEADER.index("hgnc_id")] = hgnc_id
    row[HGNC_COMPLETE_SET_HEADER.index("symbol")] = "A1BG"
    row[HGNC_COMPLETE_SET_HEADER.index("name")] = "alpha-1-B glycoprotein"
    row[HGNC_COMPLETE_SET_HEADER.index("status")] = "Approved"
    return tuple(row)


def _small_ncbi_mapping_files():
    return tuple(
        replace(definition, minimum_human_rows=1)
        for definition in NCBI_GENE_MAPPING_FILES
    )


def _ncbi_mapping_row(definition, taxon: str) -> bytes:
    values = [f"value-{index}" for index in range(len(definition.header))]
    values[definition.taxon_index] = taxon
    return ("\t".join(values) + "\n").encode()


def _ncbi_mapping_gateway(files=None) -> FakeHttpGateway:
    definitions = files or _small_ncbi_mapping_files()
    gateway = FakeHttpGateway()
    for definition in definitions:
        gateway.head_responses[definition.file.url] = HttpMetadata(
            definition.file.url,
            {"Last-Modified": "Tue, 15 Sep 2026 09:12:00 GMT"},
        )
        content = ("\t".join(definition.header) + "\n").encode()
        content += _ncbi_mapping_row(definition, "9606")
        content += _ncbi_mapping_row(definition, "10090")
        gateway.payloads[definition.file.url] = gzip.compress(content)
        gateway.download_headers[definition.file.url] = {
            "content-type": "application/x-gzip"
        }
    return gateway


def test_reactome_discovers_database_version() -> None:
    source = ReactomePathwaysSource(_reactome_gateway())

    version = source.discover_latest(VersionProbeRequest(timeout=timedelta(seconds=9)))

    assert version.value == "97"
    assert version.evidence["method"] == "reactome_database_version"


def test_ensembl_discovers_release_and_export_contract() -> None:
    exports = _small_ensembl_exports()
    source = EnsemblHumanBioMartSource(_ensembl_gateway(exports), exports=exports)

    version = source.discover_latest(VersionProbeRequest(timeout=timedelta(seconds=9)))

    assert version.value == "116-export1"
    assert version.evidence["upstream_release"] == "116"
    assert version.evidence["release_date_label"] == "Jun 2026"
    assert version.version_date == date(2026, 6, 1)


def test_ensembl_fetches_one_release_coherent_export_bundle(tmp_path: Path) -> None:
    exports = _small_ensembl_exports()
    gateway = _ensembl_gateway(exports)
    source = EnsemblHumanBioMartSource(gateway, exports=exports)

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "ensembl:human_biomart:116-export1"
    assert [str(file.relative_path) for file in snapshot.files] == [
        export.name for export in exports
    ]
    assert snapshot.metadata["species_dataset"] == "hsapiens_gene_ensembl"
    assert snapshot.metadata["taxon_id"] == 9606
    assert snapshot.metadata["exports"][exports[0].name]["rows"] == 1
    first_header = snapshot.files[0].local_path.read_text(encoding="utf-8").splitlines()[0]
    assert "Gene name" in first_header
    assert "HGNC symbol" not in first_header
    assert snapshot.metadata["exports"][exports[0].name]["endpoint"] == (
        "https://jun2026.archive.ensembl.org/biomart/martservice"
    )
    transcript_text = snapshot.files[-1].local_path.read_text(encoding="utf-8")
    assert "\t" in transcript_text.splitlines()[0]
    assert "[success]" not in transcript_text


def test_hgnc_fetches_and_validates_complete_set(tmp_path: Path) -> None:
    source = HgncCompleteSetSource(_hgnc_gateway(_hgnc_row()), minimum_rows=1)

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "hgnc:complete_set:2026-09-15"
    assert snapshot.version.version_date == date(2026, 9, 15)
    assert [str(file.relative_path) for file in snapshot.files] == [
        HGNC_COMPLETE_SET_FILE
    ]
    assert snapshot.files[0].local_path.read_text(encoding="utf-8").startswith(
        "\t".join(HGNC_COMPLETE_SET_HEADER)
    )
    assert snapshot.metadata["rows"] == 1
    assert snapshot.metadata["unique_hgnc_ids"] == 1
    assert snapshot.metadata["status_counts"] == {"Approved": 1}


def test_hgnc_rejects_duplicate_ids(tmp_path: Path) -> None:
    source = HgncCompleteSetSource(
        _hgnc_gateway(_hgnc_row(), _hgnc_row()),
        minimum_rows=1,
    )

    with pytest.raises(SourceValidationError, match="duplicate HGNC IDs"):
        source.fetch(FetchRequest(tmp_path))


def test_ensembl_rejects_incomplete_query_without_leaving_snapshot(tmp_path: Path) -> None:
    export = _small_ensembl_exports()[0]
    gateway = _ensembl_gateway((export,))
    url = biomart_url(ENSEMBL_BIOMART_ENDPOINTS[0], export.query)
    gateway.payloads[url] = ("\t".join(export.expected_header) + "\n" + "\t".join(
        "value" for _ in export.expected_header
    ) + "\n").encode()
    source = EnsemblHumanBioMartSource(
        gateway,
        exports=(export,),
        endpoints=(ENSEMBL_BIOMART_ENDPOINTS[0],),
    )

    with pytest.raises(SourceValidationError, match="completion stamp"):
        source.fetch(FetchRequest(tmp_path))

    assert not (tmp_path / "ensembl" / "human_biomart" / "116-export1").exists()


def test_ncbi_human_gene_info_preserves_and_profiles_upstream_gzip(
    tmp_path: Path,
) -> None:
    human_row = ("9606",) + tuple("value" for _ in NCBI_HUMAN_GENE_INFO_HEADER[1:])
    auxiliary_row = ("63221",) + tuple(
        "value" for _ in NCBI_HUMAN_GENE_INFO_HEADER[1:]
    )
    gateway = _ncbi_gene_info_gateway(human_row, human_row, auxiliary_row)
    source = NcbiHumanGeneInfoSource(gateway, minimum_human_rows=2)

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "ncbi:human_gene_info:2026-09-15"
    assert snapshot.version.version_date == date(2026, 9, 15)
    assert [str(file.relative_path) for file in snapshot.files] == [
        NCBI_HUMAN_GENE_INFO_FILE
    ]
    assert snapshot.metadata["rows"] == 3
    assert snapshot.metadata["taxon_counts"] == {"63221": 1, "9606": 2}
    assert gzip.decompress(snapshot.files[0].local_path.read_bytes()).decode() == (
        "\t".join(NCBI_HUMAN_GENE_INFO_HEADER)
        + "\n"
        + "\t".join(human_row)
        + "\n"
        + "\t".join(human_row)
        + "\n"
        + "\t".join(auxiliary_row)
        + "\n"
    )


def test_ncbi_human_gene_info_rejects_header_drift(tmp_path: Path) -> None:
    gateway = _ncbi_gene_info_gateway()
    gateway.payloads[NCBI_HUMAN_GENE_INFO_URL] = gzip.compress(
        b"#tax_id\tGeneID\tUnexpected\n9606\t1\tvalue\n"
    )
    source = NcbiHumanGeneInfoSource(gateway, minimum_human_rows=1)

    with pytest.raises(SourceValidationError, match="header changed"):
        source.fetch(FetchRequest(tmp_path))

    assert not (tmp_path / "ncbi" / "human_gene_info" / "2026-09-15").exists()


def test_ncbi_gene_mappings_require_one_coherent_release_date() -> None:
    files = _small_ncbi_mapping_files()
    gateway = _ncbi_mapping_gateway(files)
    gateway.head_responses[files[-1].file.url] = HttpMetadata(
        files[-1].file.url,
        {"Last-Modified": "Mon, 14 Sep 2026 23:59:00 GMT"},
    )
    source = NcbiGeneIdentifierMappingsSource(gateway, files=files)

    with pytest.raises(SourceValidationError, match="do not share one release date"):
        source.discover_latest(VersionProbeRequest())


def test_ncbi_gene_mappings_preserve_and_profile_all_files(tmp_path: Path) -> None:
    files = _small_ncbi_mapping_files()
    gateway = _ncbi_mapping_gateway(files)
    source = NcbiGeneIdentifierMappingsSource(gateway, files=files)

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "ncbi:gene_identifier_mappings:2026-09-15"
    assert snapshot.version.version_date == date(2026, 9, 15)
    assert [str(item.relative_path) for item in snapshot.files] == [
        definition.file.name for definition in files
    ]
    assert snapshot.version.evidence["method"] == "coherent_multi_file_last_modified"
    assert len(snapshot.version.evidence["files"]) == 3
    for definition in files:
        profile = snapshot.metadata["files"][definition.file.name]
        assert profile["rows"] == 2
        assert profile["human_rows"] == 1
        assert profile["nonhuman_rows"] == 1


def test_ncbi_gene_mappings_reject_wrong_taxonomy_field(tmp_path: Path) -> None:
    files = _small_ncbi_mapping_files()
    gateway = _ncbi_mapping_gateway(files)
    collaboration = files[-1]
    values = ["9606", "P12345", "not-a-taxon", "9606", "method"]
    content = ("\t".join(collaboration.header) + "\n").encode()
    content += ("\t".join(values) + "\n").encode()
    gateway.payloads[collaboration.file.url] = gzip.compress(content)
    source = NcbiGeneIdentifierMappingsSource(gateway, files=files)

    with pytest.raises(SourceValidationError, match="invalid taxonomy IDs"):
        source.fetch(FetchRequest(tmp_path))


def test_shared_last_modified_source_uses_newest_file_and_stable_names(
    tmp_path: Path,
) -> None:
    definition = LAST_MODIFIED_SOURCES["ncbi_publications"]
    gateway = FakeHttpGateway()
    dates = (
        "Tue, 01 Sep 2026 08:00:00 GMT",
        "Wed, 02 Sep 2026 08:00:00 GMT",
    )
    for file_spec, modified in zip(definition.files, dates, strict=True):
        gateway.head_responses[file_spec.url] = HttpMetadata(
            file_spec.url,
            {"Last-Modified": modified},
        )
        gateway.payloads[file_spec.url] = f"contents of {file_spec.name}\n".encode()

    source = LastModifiedHttpSource(gateway, definition)
    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.version.value == "2026-09-02"
    assert snapshot.version.version_date == date(2026, 9, 2)
    assert [str(file.relative_path) for file in snapshot.files] == [
        "gene2pubmed.gz",
        "generifs_basic.gz",
    ]
    assert snapshot.version.evidence["method"] == "multi_file_max_last_modified"


def test_inspected_file_source_uses_embedded_ctd_report_date(tmp_path: Path) -> None:
    definition = INSPECTED_FILE_SOURCES["ctd_curated_genes_diseases"]
    gateway = FakeHttpGateway()
    gateway.payloads[definition.file.url] = gzip.compress(
        b"# Report created: Tue May 28 10:15:30 EDT 2026\nGeneSymbol\tDiseaseName\n"
    )
    source = InspectedFileSource(gateway, definition)

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.version.value == "2026-05-28"
    assert snapshot.version.version_date == date(2026, 5, 28)
    assert snapshot.files[0].local_path.read_bytes() == gateway.payloads[definition.file.url]


def test_reactome_fetches_complete_versioned_snapshot(tmp_path: Path) -> None:
    gateway = _reactome_gateway()
    source = ReactomePathwaysSource(gateway)

    snapshot = FetchSource().execute(source, FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "reactome:pathways:97"
    assert snapshot.version.version_date == date(2026, 6, 20)
    assert {str(file.relative_path) for file in snapshot.files} == {
        file_spec.name for file_spec in REACTOME_FILES
    }
    assert all(
        file.local_path.parent == tmp_path / "reactome" / "pathways" / "97"
        for file in snapshot.files
    )
    assert len(gateway.download_calls) == 5


def test_reactome_does_not_publish_snapshot_without_release_date(tmp_path: Path) -> None:
    source = ReactomePathwaysSource(_reactome_gateway(last_modified=None))

    with pytest.raises(SourceValidationError, match="Last-Modified"):
        source.fetch(FetchRequest(tmp_path))

    assert not (tmp_path / "reactome" / "pathways" / "97").exists()


def test_reactome_rejects_moved_upstream_before_downloading(tmp_path: Path) -> None:
    gateway = _reactome_gateway()
    source = ReactomePathwaysSource(gateway)

    with pytest.raises(VersionMismatchError, match="upstream currently reports 97"):
        source.fetch(FetchRequest(tmp_path, expected_version=SourceVersion("96")))

    assert gateway.download_calls == []


def test_reactome_rejects_release_that_moves_during_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _reactome_gateway()
    responses = iter(
        [
            gateway.text_responses[REACTOME_VERSION_URL],
            HttpText("98", HttpMetadata(REACTOME_VERSION_URL, {})),
        ]
    )
    monkeypatch.setattr(gateway, "get_text", lambda url, timeout: next(responses))
    source = ReactomePathwaysSource(gateway)

    with pytest.raises(VersionMismatchError, match="upstream currently reports 98"):
        source.fetch(FetchRequest(tmp_path))

    assert len(gateway.download_calls) == 5
    assert not (tmp_path / "reactome" / "pathways" / "97").exists()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("02-September-2026", date(2026, 9, 2)),
        ("02-Sep-26", date(2026, 9, 2)),
        ("2026-09-02", date(2026, 9, 2)),
        (None, None),
    ],
)
def test_normalize_uniprot_release_date(raw: str | None, expected: date | None) -> None:
    assert normalize_uniprot_release_date(raw) == expected


def test_uniprot_fetches_and_validates_human_files(tmp_path: Path) -> None:
    full_records: list[dict[str, object]] = [
        {
            "primaryAccession": "P00001",
            "secondaryAccessions": ["S00001"],
            "entryType": "UniProtKB reviewed (Swiss-Prot)",
        },
        {"primaryAccession": "P00002", "entryType": "UniProtKB unreviewed (TrEMBL)"},
    ]
    reviewed_records: list[dict[str, object]] = [
        {
            "primaryAccession": "P00001",
            "secondaryAccessions": ["S00001"],
            "entryType": "UniProtKB reviewed (Swiss-Prot)",
        }
    ]
    gateway = _uniprot_gateway(full_records=full_records, reviewed_records=reviewed_records)
    source = UniProtHumanSource(gateway)

    snapshot = FetchSource().execute(source, FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "uniprot:human:2026_03"
    assert snapshot.version.version_date == date(2026, 9, 2)
    assert [str(file.relative_path) for file in snapshot.files] == [
        "uniprot-human.json.gz",
        "uniprot-human-reviewed.json.gz",
    ]
    stats = snapshot.metadata["validation"]["reviewed_in_full"]
    assert stats["full_records"] == 2
    assert stats["reviewed_records"] == 1
    assert stats["missing_reviewed_primary_accessions"] == 0


def test_uniprot_reference_proteome_preserves_validated_human_records(
    tmp_path: Path,
) -> None:
    records: list[dict[str, object]] = [
        {"primaryAccession": "P00001", "organism": {"taxonId": 9606}},
        {"primaryAccession": "P00002", "organism": {"taxonId": 9606}},
    ]
    gateway = _uniprot_gateway(full_records=[], reviewed_records=[])
    next_url = f"{UNIPROT_HUMAN_REFERENCE_PROTEOME_URL}&cursor=next"
    gateway.json_responses[UNIPROT_HUMAN_REFERENCE_PROTEOME_URL] = HttpJson(
        {"results": records[:1]},
        HttpMetadata(
            UNIPROT_HUMAN_REFERENCE_PROTEOME_URL,
            {
                "X-UniProt-Release": "2026_03",
                "X-Total-Results": "2",
                "Link": f'<{next_url}>; rel="next"',
            },
        ),
    )
    gateway.json_responses[next_url] = HttpJson(
        {"results": records[1:]},
        HttpMetadata(
            next_url,
            {
                "X-UniProt-Release": "2026_03",
                "X-Total-Results": "2",
            },
        ),
    )
    source = UniProtHumanReferenceProteomeSource(gateway, minimum_records=2)

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "uniprot:human_reference_proteome:2026_03"
    assert [str(item.relative_path) for item in snapshot.files] == [
        UNIPROT_HUMAN_REFERENCE_PROTEOME_NAME
    ]
    with gzip.open(snapshot.files[0].local_path, "rt", encoding="utf-8") as handle:
        assert json.load(handle) == {"results": records}
    assert snapshot.metadata["records"] == 2
    assert snapshot.metadata["pages"] == 2
    assert snapshot.metadata["expected_records"] == 2
    assert snapshot.metadata["unique_primary_accessions"] == 2
    assert snapshot.metadata["taxon_id"] == 9606


def test_uniprot_reference_proteome_rejects_nonhuman_records(tmp_path: Path) -> None:
    gateway = _uniprot_gateway(full_records=[], reviewed_records=[])
    gateway.json_responses[UNIPROT_HUMAN_REFERENCE_PROTEOME_URL] = HttpJson(
        {
            "results": [
                {"primaryAccession": "P00001", "organism": {"taxonId": 10090}}
            ]
        },
        HttpMetadata(
            UNIPROT_HUMAN_REFERENCE_PROTEOME_URL,
            {"X-UniProt-Release": "2026_03", "X-Total-Results": "1"},
        ),
    )
    source = UniProtHumanReferenceProteomeSource(gateway, minimum_records=1)

    with pytest.raises(SourceValidationError, match="1 non-human"):
        source.fetch(FetchRequest(tmp_path))

    assert not (
        tmp_path / "uniprot" / "human_reference_proteome" / "2026_03"
    ).exists()


def test_uniprot_reference_proteome_retries_one_failed_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _uniprot_gateway(full_records=[], reviewed_records=[])
    response = HttpJson(
        {
            "results": [
                {"primaryAccession": "P00001", "organism": {"taxonId": 9606}}
            ]
        },
        HttpMetadata(
            UNIPROT_HUMAN_REFERENCE_PROTEOME_URL,
            {"X-UniProt-Release": "2026_03", "X-Total-Results": "1"},
        ),
    )
    attempts = 0

    def get_json(url, *, timeout, headers=None):
        nonlocal attempts
        del url, timeout, headers
        attempts += 1
        if attempts == 1:
            from ifx_registry.domain.errors import SourceAcquisitionError

            raise SourceAcquisitionError("temporary timeout")
        return response

    monkeypatch.setattr(gateway, "get_json", get_json)
    delays: list[float] = []
    source = UniProtHumanReferenceProteomeSource(
        gateway,
        minimum_records=1,
        sleeper=delays.append,
    )

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.metadata["records"] == 1
    assert attempts == 2
    assert delays == [2.0]


def test_uniprot_human_idmapping_preserves_and_profiles_rows(tmp_path: Path) -> None:
    gateway = _uniprot_gateway(full_records=[], reviewed_records=[])
    payload = gzip.compress(
        b"P00001\tEnsembl\tENSG000001\n"
        b"P00001\tGeneID\t1\n"
        b"P00002-2\tRefSeq\tNP_000002.1\n"
    )
    gateway.payloads[UNIPROT_HUMAN_IDMAPPING_URL] = payload
    gateway.download_headers[UNIPROT_HUMAN_IDMAPPING_URL] = {
        "content-type": "application/x-gzip",
        "Last-Modified": "Thu, 03 Sep 2026 00:00:00 GMT",
        "ETag": '"example"',
    }
    source = UniProtHumanIdMappingSource(gateway, minimum_rows=3)

    snapshot = source.fetch(FetchRequest(tmp_path))

    assert snapshot.snapshot_id == "uniprot:human_idmapping:2026_03"
    assert [str(item.relative_path) for item in snapshot.files] == [
        UNIPROT_HUMAN_IDMAPPING_NAME
    ]
    assert snapshot.files[0].local_path.read_bytes() == payload
    assert snapshot.metadata["rows"] == 3
    assert snapshot.metadata["unique_accessions"] == 2
    assert snapshot.metadata["database_count"] == 3
    assert snapshot.metadata["download_etag"] == '"example"'


def test_uniprot_human_idmapping_rejects_malformed_rows(tmp_path: Path) -> None:
    gateway = _uniprot_gateway(full_records=[], reviewed_records=[])
    gateway.payloads[UNIPROT_HUMAN_IDMAPPING_URL] = gzip.compress(
        b"P00001\tEnsembl\n"
    )
    source = UniProtHumanIdMappingSource(gateway, minimum_rows=1)

    with pytest.raises(SourceValidationError, match="three non-empty fields"):
        source.fetch(FetchRequest(tmp_path))

    assert not (tmp_path / "uniprot" / "human_idmapping" / "2026_03").exists()


def test_uniprot_rejects_inconsistent_download_without_publishing(tmp_path: Path) -> None:
    gateway = _uniprot_gateway(
        full_records=[{"primaryAccession": "P00001"}],
        reviewed_records=[{"primaryAccession": "P99999"}],
    )
    source = UniProtHumanSource(gateway)

    with pytest.raises(SourceValidationError, match="1 primary"):
        source.fetch(FetchRequest(tmp_path))

    assert not (tmp_path / "uniprot" / "human" / "2026_03").exists()


def test_uniprot_validator_accepts_reviewed_secondary_as_full_primary(tmp_path: Path) -> None:
    full_path = tmp_path / "full.json.gz"
    reviewed_path = tmp_path / "reviewed.json.gz"
    full_path.write_bytes(_gzip_uniprot([{"primaryAccession": "P00001"}]))
    reviewed_path.write_bytes(
        _gzip_uniprot([{"primaryAccession": "P00001", "secondaryAccessions": ["P00001"]}])
    )

    stats = validate_reviewed_in_full(full_path, reviewed_path)

    assert stats["missing_reviewed_secondary_accessions"] == 0


def test_uniprot_validator_rejects_payload_without_results(tmp_path: Path) -> None:
    full_path = tmp_path / "full.json.gz"
    reviewed_path = tmp_path / "reviewed.json.gz"
    full_path.write_bytes(gzip.compress(b'{"messages": ["upstream error"]}'))
    reviewed_path.write_bytes(_gzip_uniprot([{"primaryAccession": "P00001"}]))

    with pytest.raises(SourceValidationError, match="contained no result records"):
        validate_reviewed_in_full(full_path, reviewed_path)
