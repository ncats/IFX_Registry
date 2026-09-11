"""Built-in source adapter implementations."""

from ifx_registry.infrastructure.sources.api_exports import (
    CureCaseReportsSource,
    DarkKinomeSource,
    GlyGenProteinsSource,
    LinkedOmicsGenesSource,
    ResoluteGenesSource,
)
from ifx_registry.infrastructure.sources.reactome import ReactomePathwaysSource
from ifx_registry.infrastructure.sources.uniprot import UniProtHumanSource

__all__ = [
    "CureCaseReportsSource",
    "DarkKinomeSource",
    "GlyGenProteinsSource",
    "LinkedOmicsGenesSource",
    "ReactomePathwaysSource",
    "ResoluteGenesSource",
    "UniProtHumanSource",
]
