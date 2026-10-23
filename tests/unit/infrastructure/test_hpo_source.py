"""HPO acquisition pins the ontology's declared OBO release."""

from pathlib import Path

import pytest

from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.infrastructure.http import DownloadedResource, HttpMetadata
from ifx_registry.infrastructure.sources.inspected_files import (
    INSPECTED_FILE_SOURCES,
    _hpo_version,
)


def _resource(tmp_path: Path, contents: str) -> DownloadedResource:
    path = tmp_path / "hp.obo"
    path.write_text(contents, encoding="utf-8")
    return DownloadedResource(path, HttpMetadata("https://purl.obolibrary.org/obo/hp.obo", {}))


def test_hpo_uses_official_obo_and_declared_release(tmp_path: Path) -> None:
    definition = INSPECTED_FILE_SOURCES["hpo_ontology"]
    assert str(definition.dataset) == "hpo:ontology"
    assert definition.file.url == "https://purl.obolibrary.org/obo/hp.obo"
    assert definition.file.name == "hp.obo"

    version = _hpo_version(
        _resource(
            tmp_path,
            "format-version: 1.2\n"
            "data-version: hp/releases/2026-09-01\n"
            "ontology: hp.obo\n\n[Term]\nid: HP:0000001\n",
        )
    )
    assert version.value == "2026-09-01"
    assert version.version_date.isoformat() == "2026-09-01"
    assert version.evidence == {
        "type": "obo_data_version",
        "data_version": "hp/releases/2026-09-01",
    }


@pytest.mark.parametrize(
    "header",
    [
        "ontology: hp.obo\n[Term]\n",
        "data-version: hp/releases/2026-02-30\nontology: hp.obo\n[Term]\n",
        "data-version: hp/releases/latest\nontology: hp.obo\n[Term]\n",
        "data-version: hp/releases/2026-09-01\nontology: mp.obo\n[Term]\n",
        "data-version: hp/releases/2026-09-01\nontology: hp.obo\n",
    ],
)
def test_hpo_rejects_missing_or_invalid_release(
    tmp_path: Path, header: str
) -> None:
    with pytest.raises(SourceValidationError):
        _hpo_version(_resource(tmp_path, header))
