"""Lifecycle notices must not alter immutable derived build identities."""
from dataclasses import replace

from ifx_registry.domain.derived_builds import managed_recipe_version
from ifx_registry.infrastructure.recipes import built_in_recipes


def test_only_the_two_superseded_recipes_are_deprecated() -> None:
    descriptors = [recipe.descriptor for recipe in built_in_recipes()]
    deprecated = [item for item in descriptors if item.deprecated]
    assert {str(item.dataset) for item in deprecated} == {
        "uniprot:uniref100_memberships", "ensembl:uniprot_isoform_xrefs",
    }
    for descriptor in deprecated:
        assert "Replacement not yet available" in descriptor.deprecation_message
        original = replace(descriptor, deprecated=False, deprecation_message=None)
        assert managed_recipe_version(descriptor, ()) == managed_recipe_version(original, ())
