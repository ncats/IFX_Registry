"""NCIt acquisition pins the ontology's declared OBO release."""

from pathlib import Path

import pytest

from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.infrastructure.http import DownloadedResource, HttpMetadata
from ifx_registry.infrastructure.sources.inspected_files import (
    INSPECTED_FILE_SOURCES,
    _ncit_version,
)


def _resource(tmp_path: Path, contents: str) -> DownloadedResource:
    path = tmp_path / "ncit.obo"
    path.write_text(contents, encoding="utf-8")
    return DownloadedResource(path, HttpMetadata("https://purl.obolibrary.org/obo/ncit.obo", {}))


def test_ncit_uses_official_obo_and_declared_release(tmp_path: Path) -> None:
    definition = INSPECTED_FILE_SOURCES["ncit_ontology"]
    assert str(definition.dataset) == "ncit:ontology"
    assert definition.file.url == "https://purl.obolibrary.org/obo/ncit.obo"
    assert definition.file.name == "ncit.obo"

    version = _ncit_version(
        _resource(
            tmp_path,
            "format-version: 1.2\n"
            "data-version: releases/2026-03-19\n"
            "ontology: ncit\n\n[Term]\nid: NCIT:C1000\n",
        )
    )
    assert version.value == "2026-03-19"
    assert version.version_date.isoformat() == "2026-03-19"
    assert version.evidence == {
        "type": "obo_data_version",
        "data_version": "releases/2026-03-19",
    }


@pytest.mark.parametrize(
    "header",
    [
        "ontology: ncit\n[Term]\n",
        "data-version: releases/2026-02-30\nontology: ncit\n[Term]\n",
        "data-version: releases/latest\nontology: ncit\n[Term]\n",
        "data-version: releases/2026-03-19\nontology: hp\n[Term]\n",
        "data-version: releases/2026-03-19\nontology: ncit\n",
    ],
)
def test_ncit_rejects_missing_or_invalid_release(tmp_path: Path, header: str) -> None:
    with pytest.raises(SourceValidationError):
        _ncit_version(_resource(tmp_path, header))
