"""Tests for the allowlisted YAML source catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from ifx_registry.domain.errors import SourceConfigurationError
from ifx_registry.domain.models import DatasetId
from ifx_registry.infrastructure.http import (
    DownloadedResource,
    HttpGateway,
    HttpMetadata,
    HttpText,
)
from ifx_registry.infrastructure.source_configuration import (
    DEFAULT_SOURCE_CONFIGURATION,
    YamlSourceCatalogLoader,
)
from ifx_registry.infrastructure.source_factory import BuiltInSourceFactory
from ifx_registry.presentation.web.app import WebSettings, build_services


class UnusedHttpGateway(HttpGateway):
    def get_text(self, url: str, *, timeout: float) -> HttpText:
        raise AssertionError(f"Unexpected GET for {url}")

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        raise AssertionError(f"Unexpected HEAD for {url}")

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        raise AssertionError(f"Unexpected download for {url}")


def _loader() -> YamlSourceCatalogLoader:
    return YamlSourceCatalogLoader(
        BuiltInSourceFactory(UnusedHttpGateway(), cure_api_key="test-cure-key")
    )


def test_default_configuration_installs_built_in_sources_in_display_order() -> None:
    catalog = _loader().load(DEFAULT_SOURCE_CONFIGURATION)

    descriptors = catalog.list_descriptors()

    assert [descriptor.dataset for descriptor in descriptors] == [
        DatasetId("reactome", "pathways"),
        DatasetId("uniprot", "human"),
        DatasetId("cure", "case_reports"),
        DatasetId("glygen", "proteins"),
        DatasetId("dark_kinome", "kinases"),
        DatasetId("resolute", "genes"),
        DatasetId("linkedomics", "genes"),
        DatasetId("hcop", "human_all_sixteen_column"),
        DatasetId("mgi", "hmd_human_phenotype"),
        DatasetId("impc", "genotype_phenotype_assertions"),
        DatasetId("pubtator", "gene2pubtator3"),
        DatasetId("ncbi", "publications"),
        DatasetId("ncbi", "gene_summary"),
        DatasetId("jensenlab", "tissues"),
        DatasetId("jensenlab", "diseases"),
        DatasetId("jensenlab", "protein_counts"),
        DatasetId("jensenlab", "tinx"),
        DatasetId("expasy", "enzyme"),
        DatasetId("iuphar", "ligands_interactions"),
        DatasetId("uberon", "ontology"),
        DatasetId("go", "ontology"),
        DatasetId("go", "goa_human_go"),
        DatasetId("go", "goa_human_uniprot"),
        DatasetId("mondo", "ontology"),
        DatasetId("chebi", "three_star_sdf"),
        DatasetId("chebi", "ontology_full"),
        DatasetId("bioplex", "ppi"),
        DatasetId("string", "protein_links_human"),
        DatasetId("panther", "protein_classes"),
        DatasetId("pathwaycommons", "pc_hgnc"),
        DatasetId("rhea", "reaction_bundle"),
        DatasetId("wikipathways", "human_gmt"),
        DatasetId("wikipathways", "rdf_wp"),
        DatasetId("pfocr", "human_pathways"),
        DatasetId("gtex", "expression_v11"),
        DatasetId("hpa", "tissue_expression"),
        DatasetId("tiga", "gene_trait"),
        DatasetId("surechembl", "patent_discovery"),
        DatasetId("ctd", "curated_genes_diseases"),
        DatasetId("mp", "ontology"),
        DatasetId("disease_ontology", "ontology"),
        DatasetId("refmet", "metabolites_csv"),
        DatasetId("lipidmaps", "lmsd_sdf"),
    ]
    assert [descriptor.expected_file_count for descriptor in descriptors] == [
        5,
        2,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        2,
        1,
        1,
        3,
        1,
        2,
        2,
        2,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        2,
        1,
        3,
        1,
        6,
        1,
        1,
        2,
        3,
        2,
        2,
        5,
        1,
        1,
        1,
        1,
        1,
    ]


def test_enabled_cure_source_requires_credentials(tmp_path: Path) -> None:
    configuration = tmp_path / "sources.yaml"
    configuration.write_text(
        """
schema_version: 1
sources:
  - adapter: cure_case_reports
    enabled: true
    display_name: CURE ID Case Reports
    description: Authorized reports.
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(SourceConfigurationError, match="credential file"):
        YamlSourceCatalogLoader(BuiltInSourceFactory(UnusedHttpGateway())).load(configuration)


def test_configuration_can_disable_a_source_and_control_presentation(tmp_path: Path) -> None:
    configuration = tmp_path / "sources.yaml"
    configuration.write_text(
        """
schema_version: 1
sources:
  - adapter: reactome_pathways
    enabled: false
    display_name: Hidden Reactome
    description: Not installed here.
  - adapter: uniprot_human
    enabled: true
    display_name: Human proteins
    description: Deployment-specific catalog text.
""".strip(),
        encoding="utf-8",
    )

    descriptors = _loader().load(configuration).list_descriptors()

    assert len(descriptors) == 1
    assert descriptors[0].dataset == DatasetId("uniprot", "human")
    assert descriptors[0].display_name == "Human proteins"
    assert descriptors[0].description == "Deployment-specific catalog text."


def test_web_composition_uses_the_selected_source_configuration(tmp_path: Path) -> None:
    configuration = tmp_path / "sources.yaml"
    configuration.write_text(
        """
schema_version: 1
sources:
  - adapter: reactome_pathways
    enabled: true
    display_name: Configured Reactome
    description: Loaded by the web composition root.
""".strip(),
        encoding="utf-8",
    )
    services = build_services(
        WebSettings(
            state_directory=tmp_path / "state",
            source_configuration=configuration,
        )
    )

    try:
        overviews = services.list_sources.execute()
    finally:
        services.scheduler.shutdown()

    assert len(overviews) == 1
    assert overviews[0].descriptor.display_name == "Configured Reactome"


def test_configuration_rejects_unknown_adapter_instead_of_importing_it(
    tmp_path: Path,
) -> None:
    configuration = tmp_path / "sources.yaml"
    configuration.write_text(
        """
schema_version: 1
sources:
  - adapter: some.module.ArbitraryClass
    enabled: true
    display_name: Unsafe
    description: This must not be imported.
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(SourceConfigurationError, match="unknown adapter"):
        _loader().load(configuration)


@pytest.mark.parametrize(
    "invalid_fragment",
    [
        "unexpected: value",
        "enabled: sometimes",
    ],
)
def test_configuration_rejects_unknown_fields_and_invalid_types(
    tmp_path: Path,
    invalid_fragment: str,
) -> None:
    configuration = tmp_path / "sources.yaml"
    configuration.write_text(
        f"""
schema_version: 1
sources:
  - adapter: reactome_pathways
    display_name: Reactome
    description: Pathway data.
    {invalid_fragment}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(SourceConfigurationError):
        _loader().load(configuration)
