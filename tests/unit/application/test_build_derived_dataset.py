"""End-to-end application tests for Registry-managed derived builds."""

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ifx_registry.application.derived_build_models import (
    DerivedBuildStatus,
    DerivedRecipeProduct,
    SelectedRecipeInput,
    ServiceObservation,
)
from ifx_registry.application.ports.derived_builds import (
    DerivedBuildScheduler,
    DerivedRecipe,
)
from ifx_registry.application.progress import ProgressReporter
from ifx_registry.application.use_cases.build_derived_dataset import (
    BuildDerivedDataset,
    PlanDerivedBuild,
    StartDerivedBuild,
)
from ifx_registry.domain.catalog import RegisteredSnapshotRef
from ifx_registry.domain.derived_builds import DerivedRecipeDescriptor, RecipeInputSlot
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    DerivedSnapshot,
    DerivedSnapshotFile,
    ProducerIdentity,
    SnapshotFile,
    SnapshotKind,
    SnapshotRef,
    SourceSnapshot,
    SourceVersion,
)
from ifx_registry.infrastructure.derived_build_job_store import SQLiteDerivedBuildJobStore
from ifx_registry.infrastructure.derived_recipe_catalog import InMemoryDerivedRecipeCatalog
from ifx_registry.infrastructure.local_workspaces import LocalAcquisitionWorkspaceProvider
from ifx_registry.infrastructure.materialization import FileSystemSnapshotMaterializer
from ifx_registry.infrastructure.recipes.pubchem import PubchemCidMolecularInfoRecipe
from ifx_registry.infrastructure.s3_derived_snapshots import S3DerivedSnapshotRepository
from ifx_registry.infrastructure.s3_external_versions import S3ExternalDatasetVersionRepository
from ifx_registry.infrastructure.s3_snapshots import S3PublishedSnapshotRepository

from ...fakes import FakeObjectStore


class RecordingScheduler(DerivedBuildScheduler):
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    def submit(self, job_id: str) -> None:
        self.job_ids.append(job_id)


class ObservingRecipe(DerivedRecipe):
    @property
    def descriptor(self) -> DerivedRecipeDescriptor:
        return DerivedRecipeDescriptor(
            dataset=DatasetId("example", "observed_output"),
            display_name="Observed output",
            description="Fixture with live service evidence.",
            revision="1",
            inputs=(
                RecipeInputSlot(
                    "raw",
                    "Raw input",
                    SnapshotKind.SOURCE,
                    DatasetId("example", "raw"),
                ),
            ),
            producer=_producer(),
            transform={"name": "observing_fixture", "version": 1},
        )

    def build(
        self,
        inputs,
        destination: Path,
        progress: ProgressReporter,
    ) -> DerivedRecipeProduct:
        destination.mkdir(parents=True, exist_ok=True)
        output = destination / "output.tsv"
        output.write_text("id\n1\n", encoding="utf-8")
        observed_at = datetime(2026, 9, 10, 12, tzinfo=UTC)
        return DerivedRecipeProduct(
            files=(DerivedSnapshotFile(output, PurePosixPath(output.name)),),
            validation={"rows": 1},
            observations=(
                ServiceObservation(
                    service_id="pubchem:pug_rest",
                    service_name="PubChem PUG REST",
                    interface="https",
                    operation="compound records by CID",
                    endpoint_template="https://example.org/{cids}",
                    first_observed_at=observed_at,
                    last_observed_at=observed_at,
                    request_count=2,
                    retry_count=1,
                    http_status_counts={"200": 1, "503": 1},
                    worst_throttle="yellow",
                    response_payload_sha256="c" * 64,
                ),
            ),
        )


def _producer() -> ProducerIdentity:
    return ProducerIdentity(
        "test",
        "1",
        "https://example.org/repository",
        "a" * 40,
    )


def test_build_materializes_exact_input_and_registers_output(tmp_path: Path) -> None:
    objects = FakeObjectStore()
    sources = S3PublishedSnapshotRepository(objects)
    derived = S3DerivedSnapshotRepository(objects)
    external = S3ExternalDatasetVersionRepository(objects)
    raw = tmp_path / "raw.json"
    raw.write_text("{}", encoding="utf-8")
    source = sources.publish(
        SourceSnapshot(
            DatasetId("pubchem", "raw_compounds"),
            SourceVersion("2026-09"),
            (SnapshotFile(raw, PurePosixPath("raw.json"), None),),
            datetime(2026, 9, 1, tzinfo=UTC),
        )
    )
    input_dir = tmp_path / "compound-records"
    input_dir.mkdir()
    manifest = input_dir / "pubchem_compound_records_manifest.tsv"
    manifest.write_text("batch_file\tstatus\nrecords.json.gz\tok\n", encoding="utf-8")
    batch = input_dir / "records.json.gz"
    with gzip.open(batch, "wt", encoding="utf-8") as handle:
        json.dump({"PC_Compounds": [{"id": {"id": {"cid": 7}}, "props": []}]}, handle)
    source_ref = SnapshotRef.source("pubchem:raw_compounds:2026-09")
    derived.publish(
        DerivedSnapshot(
            DatasetId("pubchem", "compound_records"),
            DatasetVersion("2026-09"),
            (
                DerivedSnapshotFile(manifest, PurePosixPath(manifest.name)),
                DerivedSnapshotFile(batch, PurePosixPath(batch.name)),
            ),
            (source_ref,),
            _producer(),
            {"name": "fixture", "version": 1},
            {"records": 1},
        ),
        (RegisteredSnapshotRef(source_ref, source.manifest_uri, source.manifest_sha256),),
    )
    recipe = PubchemCidMolecularInfoRecipe()
    recipes = InMemoryDerivedRecipeCatalog((recipe,))
    jobs = SQLiteDerivedBuildJobStore(tmp_path / "registry.sqlite3")
    scheduler = RecordingScheduler()
    planner = PlanDerivedBuild(recipes, sources, derived, external)
    start = StartDerivedBuild(planner, jobs, scheduler, derived)
    selected = SelectedRecipeInput(
        "compound_records",
        SnapshotRef.derived("pubchem:compound_records:2026-09"),
    )

    job = start.execute(recipe.descriptor.dataset, (selected,))
    BuildDerivedDataset(
        recipes,
        jobs,
        sources,
        derived,
        external,
        FileSystemSnapshotMaterializer(derived),
        derived,
        LocalAcquisitionWorkspaceProvider(tmp_path / "work"),
    ).execute(job.job_id)
    # Duplicate queue delivery is harmless after the first atomic claim completes.
    BuildDerivedDataset(
        recipes,
        jobs,
        sources,
        derived,
        external,
        FileSystemSnapshotMaterializer(derived),
        derived,
        LocalAcquisitionWorkspaceProvider(tmp_path / "work"),
    ).execute(job.job_id)

    completed = jobs.get(job.job_id)
    assert scheduler.job_ids == [job.job_id]
    assert completed.status is DerivedBuildStatus.SUCCEEDED
    output = derived.get(recipe.descriptor.dataset, job.output_version)
    assert output.inputs[0].ref == selected.reference
    assert output.validation["row_count"] == 1
    assert not (tmp_path / "work" / job.job_id).exists()


def test_build_records_typed_service_observations_in_immutable_metadata(
    tmp_path: Path,
) -> None:
    objects = FakeObjectStore()
    sources = S3PublishedSnapshotRepository(objects)
    derived = S3DerivedSnapshotRepository(objects)
    external = S3ExternalDatasetVersionRepository(objects)
    raw_file = tmp_path / "raw.tsv"
    raw_file.write_text("id\n1\n", encoding="utf-8")
    raw = sources.publish(
        SourceSnapshot(
            DatasetId("example", "raw"),
            SourceVersion("1"),
            (SnapshotFile(raw_file, PurePosixPath(raw_file.name), None),),
        )
    )
    recipe = ObservingRecipe()
    recipes = InMemoryDerivedRecipeCatalog((recipe,))
    jobs = SQLiteDerivedBuildJobStore(tmp_path / "registry.sqlite3")
    scheduler = RecordingScheduler()
    selected = SelectedRecipeInput("raw", SnapshotRef.source("example:raw:1"))
    planner = PlanDerivedBuild(recipes, sources, derived, external)
    job = StartDerivedBuild(planner, jobs, scheduler, derived).execute(
        recipe.descriptor.dataset,
        (selected,),
    )

    BuildDerivedDataset(
        recipes,
        jobs,
        sources,
        derived,
        external,
        FileSystemSnapshotMaterializer(sources),
        derived,
        LocalAcquisitionWorkspaceProvider(tmp_path / "work"),
    ).execute(job.job_id)

    published = derived.get(recipe.descriptor.dataset, job.output_version)
    observation = published.metadata["service_observations"][0]
    assert observation["service_id"] == "pubchem:pug_rest"
    assert observation["request_count"] == 2
    assert observation["retry_count"] == 1
    assert observation["first_observed_at"] == "2026-09-10T12:00:00+00:00"
    assert published.inputs[0].ref == selected.reference
    assert published.inputs[0].manifest_uri == raw.manifest_uri
