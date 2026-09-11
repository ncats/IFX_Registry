"""Installed Registry-managed derived recipe catalog."""

from ifx_registry.application.ports.derived_builds import DerivedRecipe, DerivedRecipeCatalog
from ifx_registry.domain.derived_builds import DerivedRecipeDescriptor
from ifx_registry.domain.errors import UnknownDerivedRecipeError
from ifx_registry.domain.models import DatasetId


class InMemoryDerivedRecipeCatalog(DerivedRecipeCatalog):
    def __init__(self, recipes: tuple[DerivedRecipe, ...]):
        self._recipes = {recipe.descriptor.dataset: recipe for recipe in recipes}
        if len(self._recipes) != len(recipes):
            raise ValueError("derived recipe datasets must be unique")

    def list_descriptors(self) -> tuple[DerivedRecipeDescriptor, ...]:
        return tuple(
            sorted(
                (recipe.descriptor for recipe in self._recipes.values()),
                key=lambda item: str(item.dataset),
            )
        )

    def get_recipe(self, dataset: DatasetId) -> DerivedRecipe:
        try:
            return self._recipes[dataset]
        except KeyError as error:
            raise UnknownDerivedRecipeError(
                f"No Registry build recipe is installed for {dataset}"
            ) from error
