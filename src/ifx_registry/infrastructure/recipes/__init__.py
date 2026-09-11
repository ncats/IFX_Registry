"""Built-in reusable derived dataset recipes."""

from ifx_registry.application.ports.derived_builds import DerivedRecipe
from ifx_registry.infrastructure.recipes.pubchem import (
    PubchemCidMolecularInfoRecipe,
    PubchemCompoundCidSetRecipe,
    PubchemCompoundRecordsRecipe,
)
from ifx_registry.infrastructure.recipes.surechembl import (
    SurechemblPatentFamilyMentionsRecipe,
)


def built_in_recipes() -> tuple[DerivedRecipe, ...]:
    return (
        PubchemCompoundCidSetRecipe(),
        PubchemCompoundRecordsRecipe(),
        PubchemCidMolecularInfoRecipe(),
        SurechemblPatentFamilyMentionsRecipe(),
    )


__all__ = ["built_in_recipes"]
