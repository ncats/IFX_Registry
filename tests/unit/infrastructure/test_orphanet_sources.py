"""Version and content checks for Orphadata's two independent sources."""

from pathlib import Path

import pytest

from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.infrastructure.http import DownloadedResource, HttpMetadata
from ifx_registry.infrastructure.sources.inspected_files import (
    INSPECTED_FILE_SOURCES,
    _orphanet_gene_associations_version,
)
from ifx_registry.infrastructure.sources.orphanet import (
    ORDO_BASE,
    ordo_file_url,
    parse_ordo_listing,
    validate_ordo_file,
)


def test_ordo_page_identifies_english_release_and_owl_confirms_it(tmp_path: Path) -> None:
    listing = (
        f'<a href="{ORDO_BASE}/ORDO_en_4.8.owl">old</a>'
        f'<a href="{ORDO_BASE}/ORDO_fr_4.9.owl">French</a>'
        f'<a href="{ORDO_BASE}/ORDO_en_4.9.owl">English</a>'
    )
    assert parse_ordo_listing(listing).value == "4.9"
    assert ordo_file_url("4.9").endswith("/ORDO_en_4.9.owl")
    owl = tmp_path / "ORDO_en_4.9.owl"
    owl.write_text(
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns="http://www.w3.org/2002/07/owl#">'
        '<Ontology><versionInfo>4.9</versionInfo></Ontology>'
        '<Class rdf:about="http://www.orpha.net/ORDO/Orphanet_1"/>'
        '</rdf:RDF>', encoding="utf-8",
    )
    assert validate_ordo_file(owl, "4.9") == 1
    with pytest.raises(SourceValidationError, match="does not match"):
        validate_ordo_file(owl, "5.0")


def test_orphadata_xml_uses_embedded_export_date_and_checks_completeness(
    tmp_path: Path,
) -> None:
    definition = INSPECTED_FILE_SOURCES["orphanet_gene_associations"]
    assert definition.dataset.source == "orphanet"
    assert definition.dataset.dataset == "gene_associations"
    assert definition.file.name == "orphanet_gene_associations.xml"
    xml = tmp_path / "orphanet_gene_associations.xml"
    xml.write_text(
        '<JDBOR date="2026-06-23 07:57:31"><DisorderList count="1">'
        '<Disorder id="1"/></DisorderList></JDBOR>', encoding="utf-8",
    )
    resource = DownloadedResource(xml, HttpMetadata("https://example.org", {}))
    version = _orphanet_gene_associations_version(resource)
    assert version.value == "2026-06-23"
    assert version.evidence["disorders"] == 1
    xml.write_text(xml.read_text().replace('count="1"', 'count="2"'), encoding="utf-8")
    with pytest.raises(SourceValidationError, match="declared 2"):
        _orphanet_gene_associations_version(resource)
    xml.write_text(xml.read_text()[:-8], encoding="utf-8")
    with pytest.raises(SourceValidationError, match="Could not parse"):
        _orphanet_gene_associations_version(resource)


def test_ordo_listing_requires_a_release() -> None:
    with pytest.raises(SourceValidationError, match="no English OWL release"):
        parse_ordo_listing("<html>No ORDO files</html>")
