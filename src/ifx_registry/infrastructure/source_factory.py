"""Allowlisted construction of built-in source adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.domain.errors import SourceConfigurationError
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.api_exports import (
    CureCaseReportsSource,
    DarkKinomeSource,
    GlyGenProteinsSource,
    LinkedOmicsGenesSource,
    ResoluteGenesSource,
)
from ifx_registry.infrastructure.sources.babel import (
    build_babel_gene_source,
    build_babel_protein_source,
)
from ifx_registry.infrastructure.sources.chebi import CHEBI_FILES, ChebiFullOntologySource
from ifx_registry.infrastructure.sources.ensembl import (
    ENSEMBL_EXPORTS,
    EnsemblHumanBioMartSource,
)
from ifx_registry.infrastructure.sources.hgnc import HgncCompleteSetSource
from ifx_registry.infrastructure.sources.inspected_files import (
    INSPECTED_FILE_SOURCES,
    InspectedFileSource,
    InspectedFileSourceDefinition,
)
from ifx_registry.infrastructure.sources.last_modified import (
    LAST_MODIFIED_SOURCES,
    LastModifiedHttpSource,
    LastModifiedSourceDefinition,
)
from ifx_registry.infrastructure.sources.ncbi import NcbiHumanGeneInfoSource
from ifx_registry.infrastructure.sources.ncbi_gene_mappings import (
    NCBI_GENE_MAPPING_FILES,
    NcbiGeneIdentifierMappingsSource,
)
from ifx_registry.infrastructure.sources.reactome import (
    REACTOME_FILES,
    ReactomePathwaysSource,
)
from ifx_registry.infrastructure.sources.release_sources import (
    RELEASE_SOURCES,
    ReleaseHttpSource,
    ReleaseSourceDefinition,
)
from ifx_registry.infrastructure.sources.uniprot import (
    UNIPROT_FILES,
    UniProtHumanIdMappingSource,
    UniProtHumanReferenceProteomeSource,
    UniProtHumanSource,
)
from ifx_registry.infrastructure.sources.uniprot_isoforms import (
    UniProtHumanIsoformsSource,
)


@dataclass(frozen=True, slots=True)
class ConfiguredSource:
    """A constructed adapter plus catalog information derived from its declaration."""

    adapter: SourceAdapter
    expected_file_count: int


@dataclass(frozen=True, slots=True)
class SourceFactoryDefinition:
    """Allowlisted construction rule for one source adapter type."""

    builder: Callable[[HttpGateway], SourceAdapter]
    expected_file_count: int

    def __post_init__(self) -> None:
        if self.expected_file_count <= 0:
            raise ValueError("expected_file_count must be positive")


class BuiltInSourceFactory:
    """Construct source adapters from safe configuration keys."""

    def __init__(
        self,
        http: HttpGateway,
        *,
        cure_api_key: str | None = None,
        definitions: Mapping[str, SourceFactoryDefinition] | None = None,
    ):
        self._http = http
        self._definitions = dict(
            definitions
            or {
                "reactome_pathways": SourceFactoryDefinition(
                    ReactomePathwaysSource,
                    expected_file_count=len(REACTOME_FILES),
                ),
                "uniprot_human": SourceFactoryDefinition(
                    UniProtHumanSource,
                    expected_file_count=len(UNIPROT_FILES),
                ),
                "uniprot_human_reference_proteome": SourceFactoryDefinition(
                    UniProtHumanReferenceProteomeSource,
                    expected_file_count=1,
                ),
                "uniprot_human_idmapping": SourceFactoryDefinition(
                    UniProtHumanIdMappingSource,
                    expected_file_count=1,
                ),
                "uniprot_human_isoforms": SourceFactoryDefinition(
                    UniProtHumanIsoformsSource,
                    expected_file_count=2,
                ),
                "ensembl_human_biomart": SourceFactoryDefinition(
                    EnsemblHumanBioMartSource,
                    expected_file_count=len(ENSEMBL_EXPORTS),
                ),
                "hgnc_complete_set": SourceFactoryDefinition(
                    HgncCompleteSetSource,
                    expected_file_count=1,
                ),
                "ncbi_human_gene_info": SourceFactoryDefinition(
                    NcbiHumanGeneInfoSource,
                    expected_file_count=1,
                ),
                "ncbi_gene_identifier_mappings": SourceFactoryDefinition(
                    NcbiGeneIdentifierMappingsSource,
                    expected_file_count=len(NCBI_GENE_MAPPING_FILES),
                ),
                "babel_human_gene_compendium": SourceFactoryDefinition(
                    build_babel_gene_source,
                    expected_file_count=1,
                ),
                "babel_human_protein_compendium": SourceFactoryDefinition(
                    build_babel_protein_source,
                    expected_file_count=1,
                ),
                "chebi_full_ontology": SourceFactoryDefinition(
                    ChebiFullOntologySource,
                    expected_file_count=len(CHEBI_FILES),
                ),
                "cure_case_reports": SourceFactoryDefinition(
                    _cure_builder(cure_api_key),
                    expected_file_count=1,
                ),
                "glygen_proteins": SourceFactoryDefinition(
                    GlyGenProteinsSource,
                    expected_file_count=1,
                ),
                "dark_kinome_kinases": SourceFactoryDefinition(
                    DarkKinomeSource,
                    expected_file_count=1,
                ),
                "resolute_genes": SourceFactoryDefinition(
                    ResoluteGenesSource,
                    expected_file_count=1,
                ),
                "linkedomics_genes": SourceFactoryDefinition(
                    LinkedOmicsGenesSource,
                    expected_file_count=1,
                ),
                **{
                    name: SourceFactoryDefinition(
                        _last_modified_builder(definition),
                        expected_file_count=len(definition.files),
                    )
                    for name, definition in LAST_MODIFIED_SOURCES.items()
                },
                **{
                    name: SourceFactoryDefinition(
                        _release_builder(definition),
                        expected_file_count=definition.expected_file_count,
                    )
                    for name, definition in RELEASE_SOURCES.items()
                },
                **{
                    name: SourceFactoryDefinition(
                        _inspected_file_builder(definition),
                        expected_file_count=1,
                    )
                    for name, definition in INSPECTED_FILE_SOURCES.items()
                },
            }
        )

    @property
    def adapter_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def is_registered(self, adapter_name: str) -> bool:
        return adapter_name in self._definitions

    def create(self, adapter_name: str) -> ConfiguredSource:
        try:
            definition = self._definitions[adapter_name]
        except KeyError as error:
            available = ", ".join(self.adapter_names) or "none"
            raise SourceConfigurationError(
                f"Unknown source adapter {adapter_name!r}; available adapters: {available}"
            ) from error
        adapter = definition.builder(self._http)
        return ConfiguredSource(
            adapter=adapter,
            expected_file_count=definition.expected_file_count,
        )


def _cure_builder(api_key: str | None) -> Callable[[HttpGateway], SourceAdapter]:
    def build(http: HttpGateway) -> SourceAdapter:
        if api_key is None:
            raise SourceConfigurationError(
                "CURE ID is enabled but its credential file is not configured"
            )
        return CureCaseReportsSource(http, api_key)

    return build


def _last_modified_builder(
    definition: LastModifiedSourceDefinition,
) -> Callable[[HttpGateway], SourceAdapter]:
    def build(http: HttpGateway) -> SourceAdapter:
        return LastModifiedHttpSource(http, definition)

    return build


def _release_builder(
    definition: ReleaseSourceDefinition,
) -> Callable[[HttpGateway], SourceAdapter]:
    def build(http: HttpGateway) -> SourceAdapter:
        return ReleaseHttpSource(http, definition)

    return build


def _inspected_file_builder(
    definition: InspectedFileSourceDefinition,
) -> Callable[[HttpGateway], SourceAdapter]:
    def build(http: HttpGateway) -> SourceAdapter:
        return InspectedFileSource(http, definition)

    return build
