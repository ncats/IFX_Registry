"""Validate, schedule, execute, and register reusable derived dataset builds."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ifx_registry.application.derived_build_models import (
    DerivedBuildJob,
    DerivedBuildPlan,
    DerivedBuildStatus,
    MaterializedRecipeInput,
    SelectedRecipeInput,
)
from ifx_registry.application.ports.derived_builds import (
    DerivedBuildJobStore,
    DerivedBuildScheduler,
    DerivedRecipeCatalog,
)
from ifx_registry.application.ports.external import ExternalDatasetVersionCatalog
from ifx_registry.application.ports.snapshots import (
    DerivedSnapshotPublisher,
    JobWorkspaceProvider,
    PublishedDerivedSnapshotCatalog,
    PublishedSnapshotCatalog,
    SnapshotMaterializationCache,
)
from ifx_registry.application.progress import ProgressReporter, ProgressUpdate
from ifx_registry.domain.catalog import (
    PublishedDatasetSnapshot,
    PublishedDerivedSnapshot,
    PublishedExternalDatasetVersion,
    PublishedSnapshot,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.derived_builds import (
    RecipeInputSlot,
    effective_recipe_transform,
    managed_recipe_version,
)
from ifx_registry.domain.errors import (
    DerivedBuildAlreadyRunningError,
    InvalidDerivedBuildError,
    RegistryError,
    SnapshotAlreadyExistsError,
    SnapshotNotFoundError,
)
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    DerivedSnapshot,
    SnapshotKind,
    SnapshotRef,
)


class PlanDerivedBuild:
    def __init__(
        self,
        recipes: DerivedRecipeCatalog,
        sources: PublishedSnapshotCatalog,
        derived: PublishedDerivedSnapshotCatalog,
        external: ExternalDatasetVersionCatalog,
    ):
        self._recipes = recipes
        self._sources = sources
        self._derived = derived
        self._external = external

    def execute(
        self,
        dataset: DatasetId,
        inputs: tuple[SelectedRecipeInput, ...],
    ) -> DerivedBuildPlan:
        recipe = self._recipes.get_recipe(dataset)
        self._validate_inputs(recipe.descriptor.inputs, inputs)
        selected_by_slot = {item.slot: item for item in inputs}
        ordered_inputs = tuple(
            selected_by_slot[slot.name] for slot in recipe.descriptor.inputs
        )
        registered = tuple(
            self._registered_input(selected)
            for selected in ordered_inputs
        )
        output_version = managed_recipe_version(recipe.descriptor, registered).value
        return DerivedBuildPlan(
            dataset=dataset,
            output_version=output_version,
            recipe_revision=recipe.descriptor.revision,
            inputs=ordered_inputs,
            registered_inputs=registered,
        )

    def _registered_input(self, selected: SelectedRecipeInput) -> RegisteredSnapshotRef:
        published = self._get_snapshot(selected.reference)
        return RegisteredSnapshotRef(
            selected.reference,
            published.manifest_uri,
            published.manifest_sha256,
            selected.slot,
        )

    def _get_snapshot(
        self,
        reference: SnapshotRef,
    ) -> PublishedDatasetSnapshot | PublishedExternalDatasetVersion:
        if reference.kind is SnapshotKind.SOURCE:
            return self._sources.get(reference.dataset, reference.version.value)
        if reference.kind is SnapshotKind.DERIVED:
            return self._derived.get(reference.dataset, reference.version.value)
        return self._external.get(reference.dataset, reference.version.value)

    @staticmethod
    def _validate_inputs(
        slots: tuple[RecipeInputSlot, ...],
        inputs: tuple[SelectedRecipeInput, ...],
    ) -> None:
        expected = {slot.name: slot for slot in slots}
        selected = {item.slot: item for item in inputs}
        if len(selected) != len(inputs) or set(selected) != set(expected):
            raise InvalidDerivedBuildError(
                "Build inputs must select exactly one version for every recipe input"
            )
        for name, item in selected.items():
            slot = expected[name]
            if (
                item.reference.kind is not slot.kind
                or item.reference.dataset != slot.dataset
            ):
                raise InvalidDerivedBuildError(
                    f"Input {name} must select {slot.kind.value} {slot.dataset}"
                )


class StartDerivedBuild:
    def __init__(
        self,
        planner: PlanDerivedBuild,
        jobs: DerivedBuildJobStore,
        scheduler: DerivedBuildScheduler,
        derived: PublishedDerivedSnapshotCatalog,
    ):
        self._planner = planner
        self._jobs = jobs
        self._scheduler = scheduler
        self._derived = derived

    def execute(
        self,
        dataset: DatasetId,
        inputs: tuple[SelectedRecipeInput, ...],
    ) -> DerivedBuildJob:
        plan = self._planner.execute(dataset, inputs)
        try:
            self._derived.get(dataset, plan.output_version)
        except SnapshotNotFoundError:
            pass
        else:
            raise SnapshotAlreadyExistsError(
                f"Derived version is already registered: {dataset}:{plan.output_version}"
            )
        active = self._jobs.find_active(dataset)
        if active is not None:
            raise DerivedBuildAlreadyRunningError(
                f"Build {active.job_id} is already active for {dataset}"
            )
        job = DerivedBuildJob(
            job_id=uuid4().hex,
            dataset=dataset,
            output_version=plan.output_version,
            recipe_revision=plan.recipe_revision,
            inputs=plan.inputs,
        )
        self._jobs.add(job)
        try:
            self._scheduler.submit(job.job_id)
        except Exception as error:
            failed_at = datetime.now(UTC)
            self._jobs.save(
                replace(
                    job,
                    status=DerivedBuildStatus.FAILED,
                    stage="failed",
                    message="Could not schedule build",
                    error=str(error),
                    updated_at=failed_at,
                    completed_at=failed_at,
                )
            )
            raise
        return job


class _BuildProgressReporter(ProgressReporter):
    def __init__(self, jobs: DerivedBuildJobStore, job_id: str):
        self._jobs = jobs
        self._job_id = job_id

    def report(self, update: ProgressUpdate) -> None:
        job = self._jobs.get(self._job_id)
        message = update.message
        if update.completed is not None and update.total is not None:
            message = f"{message} ({update.completed}/{update.total})"
        self._jobs.save(
            replace(
                job,
                stage=update.stage,
                message=message,
                updated_at=datetime.now(UTC),
            )
        )


class BuildDerivedDataset:
    def __init__(
        self,
        recipes: DerivedRecipeCatalog,
        jobs: DerivedBuildJobStore,
        sources: PublishedSnapshotCatalog,
        derived: PublishedDerivedSnapshotCatalog,
        external: ExternalDatasetVersionCatalog,
        materializer: SnapshotMaterializationCache,
        publisher: DerivedSnapshotPublisher,
        workspaces: JobWorkspaceProvider,
    ):
        self._recipes = recipes
        self._jobs = jobs
        self._sources = sources
        self._derived = derived
        self._external = external
        self._materializer = materializer
        self._publisher = publisher
        self._workspaces = workspaces

    def execute(self, job_id: str) -> None:
        job = self._jobs.claim(job_id)
        if job is None:
            return
        try:
            recipe = self._recipes.get_recipe(job.dataset)
            if recipe.descriptor.revision != job.recipe_revision:
                raise InvalidDerivedBuildError(
                    "The installed recipe changed after this build was queued"
                )
            reporter = _BuildProgressReporter(self._jobs, job_id)
            with self._workspaces.open(job_id) as workspace:
                materialized, registered = self._materialize_inputs(
                    recipe.descriptor.inputs,
                    job.inputs,
                    workspace,
                )
                expected_version = managed_recipe_version(
                    recipe.descriptor,
                    registered,
                ).value
                if job.output_version != expected_version:
                    raise InvalidDerivedBuildError(
                        "The queued build identity no longer matches its exact inputs"
                    )
                reporter.report(ProgressUpdate("building", "Running derived recipe"))
                product = recipe.build(
                    materialized,
                    workspace / "output",
                    reporter,
                )
                reporter.report(
                    ProgressUpdate(
                        "registering",
                        "Validating and registering derived files",
                    )
                )
                metadata = dict(product.metadata)
                if "service_observations" in metadata:
                    raise InvalidDerivedBuildError(
                        "Recipe metadata cannot supply the reserved service_observations field"
                    )
                if product.observations:
                    metadata["service_observations"] = [
                        observation.as_metadata()
                        for observation in product.observations
                    ]
                snapshot = DerivedSnapshot(
                    dataset=job.dataset,
                    version=DatasetVersion(
                        job.output_version,
                        version_date=product.version_date,
                    ),
                    files=product.files,
                    inputs=tuple(item.ref for item in registered),
                    producer=recipe.descriptor.producer,
                    transform=effective_recipe_transform(recipe.descriptor),
                    validation=product.validation,
                    metadata=metadata,
                )
                published = self._publisher.publish(snapshot, registered)
            completed_at = datetime.now(UTC)
            self._jobs.save(
                replace(
                    self._jobs.get(job_id),
                    status=DerivedBuildStatus.SUCCEEDED,
                    stage="complete",
                    message="Built, validated, and registered in the Registry",
                    snapshot_id=published.snapshot_id,
                    updated_at=completed_at,
                    completed_at=completed_at,
                )
            )
        except Exception as error:
            failed_at = datetime.now(UTC)
            self._jobs.save(
                replace(
                    self._jobs.get(job_id),
                    status=DerivedBuildStatus.FAILED,
                    stage="failed",
                    message="Derived build failed",
                    error=(
                        str(error)
                        if isinstance(error, RegistryError)
                        else f"{type(error).__name__}: {error}"
                    ),
                    updated_at=failed_at,
                    completed_at=failed_at,
                )
            )

    def _materialize_inputs(
        self,
        slots: tuple[RecipeInputSlot, ...],
        selected: tuple[SelectedRecipeInput, ...],
        workspace: Path,
    ) -> tuple[
        dict[str, MaterializedRecipeInput],
        tuple[RegisteredSnapshotRef, ...],
    ]:
        slot_by_name = {slot.name: slot for slot in slots}
        materialized: dict[str, MaterializedRecipeInput] = {}
        registered: list[RegisteredSnapshotRef] = []
        for item in selected:
            slot = slot_by_name[item.slot]
            published = self._published_input(item)
            reference = RegisteredSnapshotRef(
                item.reference,
                published.manifest_uri,
                published.manifest_sha256,
                item.slot,
            )
            local_directory = None
            if isinstance(published, (PublishedSnapshot, PublishedDerivedSnapshot)):
                local_directory = self._materializer.materialize(
                    published,
                    workspace / "inputs" / item.slot,
                ).local_dir
            materialized[item.slot] = MaterializedRecipeInput(
                slot,
                reference,
                local_directory,
            )
            registered.append(reference)
        return materialized, tuple(registered)

    def _published_input(
        self,
        selected: SelectedRecipeInput,
    ) -> PublishedDatasetSnapshot | PublishedExternalDatasetVersion:
        reference = selected.reference
        if reference.kind is SnapshotKind.SOURCE:
            return self._sources.get(reference.dataset, reference.version.value)
        if reference.kind is SnapshotKind.DERIVED:
            return self._derived.get(reference.dataset, reference.version.value)
        return self._external.get(reference.dataset, reference.version.value)
