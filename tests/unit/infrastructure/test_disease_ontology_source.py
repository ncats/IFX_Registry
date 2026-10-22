"""Disease Ontology acquisition reads the OBO release header."""

from pathlib import Path

import pytest

from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.infrastructure.http import DownloadedResource, HttpMetadata
from ifx_registry.infrastructure.sources.inspected_files import (
    INSPECTED_FILE_SOURCES,
    _disease_ontology_version,
)


def _resource(tmp_path: Path, contents: str) -> DownloadedResource:
    path = tmp_path / "doid.obo"
    path.write_text(contents, encoding="utf-8")
    return DownloadedResource(path, HttpMetadata("https://purl.obolibrary.org/obo/doid.obo", {}))


def test_disease_ontology_uses_obo_filename_and_release_header(tmp_path: Path) -> None:
    definition = INSPECTED_FILE_SOURCES["disease_ontology"]
    assert definition.file.url == "https://purl.obolibrary.org/obo/doid.obo"
    assert definition.file.name == "doid.obo"

    version = _disease_ontology_version(
        _resource(tmp_path, "format-version: 1.2\ndata-version: releases/2026-09-30/doid.obo\n\n[Term]\n")
    )
    assert version.value == "2026-09-30"
    assert version.evidence == {
        "type": "obo_data_version",
        "data_version": "releases/2026-09-30/doid.obo",
    }


@pytest.mark.parametrize(
    "header",
    [
        "format-version: 1.2\n[Term]\n",
        "data-version: releases/2026-02-30/doid.obo\n[Term]\n",
        "data-version: releases/2026-09-30/other.obo\n[Term]\n",
    ],
)
def test_disease_ontology_rejects_missing_or_invalid_release_header(
    tmp_path: Path, header: str
) -> None:
    with pytest.raises(SourceValidationError):
        _disease_ontology_version(_resource(tmp_path, header))
