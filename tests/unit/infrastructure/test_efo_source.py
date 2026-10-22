"""EFO acquisition requires a versioned OBO header."""

from pathlib import Path

import pytest

from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.infrastructure.http import DownloadedResource, HttpMetadata
from ifx_registry.infrastructure.sources.inspected_files import (
    INSPECTED_FILE_SOURCES,
    _efo_version,
)


def _resource(tmp_path: Path, contents: str) -> DownloadedResource:
    path = tmp_path / "efo.obo"
    path.write_text(contents, encoding="utf-8")
    return DownloadedResource(path, HttpMetadata("https://www.ebi.ac.uk/efo/efo.obo", {}))


def test_efo_uses_obo_filename_and_release_header(tmp_path: Path) -> None:
    definition = INSPECTED_FILE_SOURCES["efo_ontology"]
    assert definition.dataset.source == "efo"
    assert definition.dataset.dataset == "ontology"
    assert definition.file.url == "https://www.ebi.ac.uk/efo/efo.obo"
    assert definition.file.name == "efo.obo"

    version = _efo_version(
        _resource(
            tmp_path,
            "format-version: 1.2\n"
            "data-version: http://www.ebi.ac.uk/efo/releases/v3.95.0/efo.owl\n"
            "\n[Term]\n",
        )
    )
    assert version.value == "v3.95.0"
    assert version.evidence == {
        "type": "obo_data_version",
        "data_version": "http://www.ebi.ac.uk/efo/releases/v3.95.0/efo.owl",
    }


@pytest.mark.parametrize(
    "header",
    [
        "format-version: 1.2\n[Term]\n",
        "data-version: http://www.ebi.ac.uk/efo/releases/current/efo.owl\n[Term]\n",
        "data-version: http://www.ebi.ac.uk/efo/releases/v3.95.0/other.owl\n[Term]\n",
    ],
)
def test_efo_rejects_missing_or_invalid_release_header(
    tmp_path: Path, header: str
) -> None:
    with pytest.raises(SourceValidationError):
        _efo_version(_resource(tmp_path, header))
