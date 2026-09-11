"""Ports for Registry-managed derived recipes and durable build jobs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path

from ifx_registry.application.derived_build_models import (
    DerivedBuildJob,
    DerivedRecipeProduct,
    MaterializedRecipeInput,
)
from ifx_registry.application.progress import ProgressReporter
from ifx_registry.domain.derived_builds import DerivedRecipeDescriptor
from ifx_registry.domain.models import DatasetId


class DerivedRecipe(ABC):
    @property
    @abstractmethod
    def descriptor(self) -> DerivedRecipeDescriptor:
        """Describe the recipe and its legal exact inputs."""

    @abstractmethod
    def build(
        self,
        inputs: Mapping[str, MaterializedRecipeInput],
        destination: Path,
        progress: ProgressReporter,
    ) -> DerivedRecipeProduct:
        """Build and validate files inside the caller-owned workspace."""

class DerivedRecipeCatalog(ABC):
    @abstractmethod
    def list_descriptors(self) -> tuple[DerivedRecipeDescriptor, ...]:
        """List every installed reusable recipe."""

    @abstractmethod
    def get_recipe(self, dataset: DatasetId) -> DerivedRecipe:
        """Return the installed recipe for one derived dataset."""


class DerivedBuildJobStore(ABC):
    @abstractmethod
    def add(self, job: DerivedBuildJob) -> None: ...

    @abstractmethod
    def get(self, job_id: str) -> DerivedBuildJob: ...

    @abstractmethod
    def save(self, job: DerivedBuildJob) -> None: ...

    @abstractmethod
    def claim(self, job_id: str) -> DerivedBuildJob | None:
        """Atomically transition one queued job to running."""

    @abstractmethod
    def find_active(self, dataset: DatasetId) -> DerivedBuildJob | None: ...

    @abstractmethod
    def find_latest(self, dataset: DatasetId) -> DerivedBuildJob | None: ...

    @abstractmethod
    def list_active(self) -> tuple[DerivedBuildJob, ...]: ...

    @abstractmethod
    def list_attention(self) -> tuple[DerivedBuildJob, ...]:
        """List active jobs plus the latest failed job for each dataset."""

    @abstractmethod
    def recover_interrupted(self) -> None:
        """Fail work left active by the single-process executor after restart."""


class DerivedBuildScheduler(ABC):
    @abstractmethod
    def submit(self, job_id: str) -> None: ...
