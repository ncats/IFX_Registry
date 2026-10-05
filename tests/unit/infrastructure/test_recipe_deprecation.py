"""Lifecycle notices must not alter immutable derived build identities."""

from dataclasses import replace

from ifx_registry.domain.derived_builds import managed_recipe_version
from ifx_registry.infrastructure.recipes import built_in_recipes


def test_only_the_two_superseded_recipes_are_deprecated() -> None:
    descriptors = [recipe.descriptor for recipe in built_in_recipes()]
    deprecated = [item for item in descriptors if item.deprecated]
    assert {str(item.dataset) for item in deprecated} == {
        "uniprot:uniref100_memberships",
        "ensembl:uniprot_isoform_xrefs",
    }
    for descriptor in deprecated:
        assert descriptor.replacement is not None
        assert "Replacement not yet available" not in descriptor.deprecation_message
        original = replace(descriptor, deprecated=False, deprecation_message=None, replacement=None)
        assert managed_recipe_version(descriptor, ()) == managed_recipe_version(original, ())


def test_replacements_are_installed_sources():
    from ifx_registry.infrastructure.source_configuration import (
        DEFAULT_SOURCE_CONFIGURATION,
        YamlSourceCatalogLoader,
    )
    from ifx_registry.infrastructure.source_factory import BuiltInSourceFactory
    from tests.unit.infrastructure.test_source_configuration import UnusedHttpGateway

    catalog = YamlSourceCatalogLoader(
        BuiltInSourceFactory(UnusedHttpGateway(), cure_api_key="test")
    ).load(DEFAULT_SOURCE_CONFIGURATION)
    installed = {descriptor.dataset for descriptor in catalog.list_descriptors()}
    for recipe in built_in_recipes():
        if recipe.descriptor.replacement is not None:
            assert recipe.descriptor.replacement in installed
