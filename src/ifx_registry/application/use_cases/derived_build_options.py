"""Describe legal exact inputs for a Registry-managed derived build."""

from __future__ import annotations

from dataclasses import dataclass

from ifx_registry.application.derived_build_models import (
    DerivedBuildJob,
    DerivedBuildStatus,
    SelectedRecipeInput,
)
from ifx_registry.application.ports.derived_builds import (
    DerivedBuildJobStore,
    DerivedRecipeCatalog,
)
from ifx_registry.application.ports.external import ExternalDatasetVersionCatalog
from ifx_registry.application.ports.snapshots import (
    PublishedDerivedSnapshotCatalog,
    PublishedSnapshotCatalog,
)
from ifx_registry.application.use_cases.browse_catalog import CatalogVersion
from ifx_registry.application.use_cases.build_derived_dataset import PlanDerivedBuild
from ifx_registry.domain.derived_builds import DerivedRecipeDescriptor, RecipeInputSlot
from ifx_registry.domain.errors import UnknownDerivedRecipeError
from ifx_registry.domain.models import DatasetId, DatasetVersion, SnapshotKind, SnapshotRef


@dataclass(frozen=True, slots=True)
class RecipeInputOptions:
    slot: RecipeInputSlot
    versions: tuple[CatalogVersion, ...]


@dataclass(frozen=True, slots=True)
class DerivedBuildOptions:
    recipe: DerivedRecipeDescriptor
    inputs: tuple[RecipeInputOptions, ...]
    suggested_version: str
    active_job: DerivedBuildJob | None
    latest_job: DerivedBuildJob | None
    registered_output_versions: frozenset[str]

    @property
    def can_build(self) -> bool:
        return all(item.versions for item in self.inputs) and self.active_job is None

    @property
    def failed_job(self) -> DerivedBuildJob | None:
        if self.latest_job is not None and self.latest_job.status is DerivedBuildStatus.FAILED:
            return self.latest_job
        return None

    @property
    def default_inputs(self) -> dict[str, str]:
        if self.failed_job is not None:
            return {
                item.slot: item.reference.version.value for item in self.failed_job.inputs
            }
        return {
            item.slot.name: item.versions[0].version.value
            for item in self.inputs
            if item.versions
        }

    @property
    def default_output_version(self) -> str:
        return self.failed_job.output_version if self.failed_job else self.suggested_version


class GetDerivedBuildOptions:
    def __init__(
        self,
        recipes: DerivedRecipeCatalog,
        jobs: DerivedBuildJobStore,
        sources: PublishedSnapshotCatalog,
        derived: PublishedDerivedSnapshotCatalog,
        external: ExternalDatasetVersionCatalog,
        planner: PlanDerivedBuild,
    ):
        self._recipes = recipes
        self._jobs = jobs
        self._sources = sources
        self._derived = derived
        self._external = external
        self._planner = planner

    def execute(self, dataset: DatasetId) -> DerivedBuildOptions | None:
        try:
            recipe = self._recipes.get_recipe(dataset)
        except UnknownDerivedRecipeError:
            return None
        versions_by_kind: dict[SnapshotKind, tuple[CatalogVersion, ...]] = {
            SnapshotKind.SOURCE: self._sources.list_all(),
            SnapshotKind.DERIVED: self._derived.list_all(),
            SnapshotKind.EXTERNAL: self._external.list_all(),
        }
        options = tuple(
            RecipeInputOptions(
                slot,
                tuple(
                    sorted(
                        (
                            item
                            for item in versions_by_kind[slot.kind]
                            if item.dataset == slot.dataset
                        ),
                        key=lambda item: item.published_at,
                        reverse=True,
                    )
                ),
            )
            for slot in recipe.descriptor.inputs
        )
        selected = tuple(
            SelectedRecipeInput(
                item.slot.name,
                SnapshotRef(
                    item.slot.kind,
                    item.slot.dataset,
                    DatasetVersion(item.versions[0].version.value),
                ),
            )
            for item in options
            if item.versions
        )
        suggested = (
            self._planner.execute(dataset, selected).output_version
            if len(selected) == len(options)
            else ""
        )
        active_job = self._jobs.find_active(dataset)
        return DerivedBuildOptions(
            recipe.descriptor,
            options,
            suggested,
            active_job,
            self._jobs.find_latest(dataset),
            frozenset(
                item.version.value
                for item in versions_by_kind[SnapshotKind.DERIVED]
                if item.dataset == dataset
            ),
        )
