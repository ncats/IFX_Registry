"""Built-in reusable derived dataset recipes."""

from ifx_registry.application.ports.derived_builds import DerivedRecipe
from ifx_registry.infrastructure.recipes.ensembl_uniprot_isoforms import (
    EnsemblUniProtIsoformXrefsRecipe,
)
from ifx_registry.infrastructure.recipes.ncbi_gene_mappings import (
    NcbiHumanGeneIdentifierMappingsRecipe,
)
from ifx_registry.infrastructure.recipes.pubchem import (
    PubchemCidMolecularInfoRecipe,
    PubchemCompoundCidSetRecipe,
    PubchemCompoundRecordsRecipe,
)
from ifx_registry.infrastructure.recipes.surechembl import (
    SurechemblPatentFamilyMentionsRecipe,
)
from ifx_registry.infrastructure.recipes.uniref100 import UniRef100MembershipsRecipe


def built_in_recipes() -> tuple[DerivedRecipe, ...]:
    return (
        PubchemCompoundCidSetRecipe(),
        PubchemCompoundRecordsRecipe(),
        PubchemCidMolecularInfoRecipe(),
        NcbiHumanGeneIdentifierMappingsRecipe(),
        EnsemblUniProtIsoformXrefsRecipe(),
        UniRef100MembershipsRecipe(),
        SurechemblPatentFamilyMentionsRecipe(),
    )


__all__ = ["built_in_recipes"]
