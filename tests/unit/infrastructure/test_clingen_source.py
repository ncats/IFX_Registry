"""ClinGen gene-validity acquisition uses its embedded CSV creation date."""

from pathlib import Path
import hashlib

import pytest

from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.infrastructure.http import DownloadedResource, HttpMetadata
from ifx_registry.infrastructure.sources.inspected_files import (
    INSPECTED_FILE_SOURCES,
    _clingen_version,
)


URL = "https://search.clinicalgenome.org/kb/gene-validity/download"
VALID = (
    '"CLINGEN GENE DISEASE VALIDITY CURATIONS",""\n'
    '"FILE CREATED: 2026-10-22",""\n'
    '"WEBPAGE: https://search.clinicalgenome.org/kb/gene-validity",""\n'
    '"+++++++++++","++++++++++++++"\n'
    '"GENE SYMBOL","GENE ID (HGNC)","DISEASE LABEL",'
    '"DISEASE ID (MONDO)","MOI","SOP","CLASSIFICATION",'
    '"ONLINE REPORT","CLASSIFICATION DATE","GCEP"\n'
    '"+++++++++++","++++++++++++++"\n'
    '"AARS1","HGNC:20","AARS1-related disease","MONDO:123",'
    '"AR","SOP11","Definitive","report","2026-10-22","Panel"\n'
)


def _resource(tmp_path: Path, contents: str) -> DownloadedResource:
    path = tmp_path / "clingen_gene_validity.csv"
    path.write_text(contents, encoding="utf-8")
    return DownloadedResource(path, HttpMetadata(URL, {}))


def test_clingen_uses_csv_file_created_date_and_filename(tmp_path: Path) -> None:
    definition = INSPECTED_FILE_SOURCES["clingen_gene_validity"]
    assert definition.dataset.source == "clingen"
    assert definition.dataset.dataset == "gene_validity"
    assert definition.file.url == URL
    assert definition.file.name == "clingen_gene_validity.csv"

    version = _clingen_version(_resource(tmp_path, VALID))
    digest = hashlib.sha256(VALID.encode()).hexdigest()
    assert version.value == f"2026-10-22-sha256-{digest[:12]}"
    assert version.version_date.isoformat() == "2026-10-22"
    assert version.evidence == {
        "type": "csv_file_created_and_content_hash",
        "file_created": "2026-10-22",
        "sha256": digest,
    }


@pytest.mark.parametrize(
    "contents",
    [
        VALID.replace("FILE CREATED: 2026-10-22", "FILE CREATED: latest"),
        VALID.replace("FILE CREATED: 2026-10-22", "FILE CREATED: 2026-02-30"),
        VALID.replace("GENE ID (HGNC)", "other column"),
        VALID.replace('"AARS1","HGNC:20","AARS1-related disease"', '"AARS1"'),
        VALID.split('"GENE SYMBOL"')[0],
    ],
)
def test_clingen_rejects_invalid_or_incomplete_export(
    tmp_path: Path, contents: str
) -> None:
    with pytest.raises(SourceValidationError):
        _clingen_version(_resource(tmp_path, contents))
