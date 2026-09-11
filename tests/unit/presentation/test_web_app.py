"""End-to-end tests for the small server-rendered Registry UI."""

import re
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import anyio
import httpx
import pytest

import ifx_registry.presentation.web.app as web_app_module
from ifx_registry import ExternalDatasetVersion, ProducerIdentity, SnapshotRef
from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.application.ports.derived_builds import DerivedRecipe
from ifx_registry.application.ports.source import SourceAdapter
from ifx_registry.application.progress import ProgressUpdate
from ifx_registry.application.use_cases.acquire_source import (
    AcquireSourceSnapshot,
    StartSourceAcquisition,
)
from ifx_registry.application.use_cases.acquisition_queries import AcquisitionQueries
from ifx_registry.application.use_cases.assess_source_update import AssessSourceUpdate
from ifx_registry.application.use_cases.browse_catalog import (
    BrowsePublishedCatalog,
    BrowseRegistryCatalog,
    GetPublishedDataset,
    GetPublishedSnapshot,
    GetRegistryDataset,
    GetRegistryVersion,
)
from ifx_registry.application.use_cases.build_derived_dataset import (
    PlanDerivedBuild,
    StartDerivedBuild,
)
from ifx_registry.application.use_cases.check_source_version import CheckSourceVersion
from ifx_registry.application.use_cases.dataset_lineage import GetRegistryDatasetDetails
from ifx_registry.application.use_cases.derived_build_options import GetDerivedBuildOptions
from ifx_registry.application.use_cases.derived_build_queries import DerivedBuildQueries
from ifx_registry.application.use_cases.list_sources import ListSources
from ifx_registry.application.use_cases.source_check_status import ListSourceCheckStatuses
from ifx_registry.domain.catalog import RegisteredSnapshotRef, SourceDescriptor
from ifx_registry.domain.derived_builds import DerivedRecipeDescriptor, RecipeInputSlot
from ifx_registry.domain.errors import (
    OperationalStateUnavailableError,
    RegistryUnavailableError,
    SourceValidationError,
)
from ifx_registry.domain.jobs import AcquisitionJob
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    DerivedSnapshot,
    DerivedSnapshotFile,
    SnapshotFile,
    SnapshotKind,
    SourceSnapshot,
    SourceVersion,
)
from ifx_registry.infrastructure.catalog import InMemorySourceCatalog
from ifx_registry.infrastructure.derived_build_job_store import SQLiteDerivedBuildJobStore
from ifx_registry.infrastructure.derived_recipe_catalog import InMemoryDerivedRecipeCatalog
from ifx_registry.infrastructure.job_store import SQLiteAcquisitionJobStore
from ifx_registry.infrastructure.local_workspaces import LocalAcquisitionWorkspaceProvider
from ifx_registry.infrastructure.recipes.pubchem import PubchemCidMolecularInfoRecipe
from ifx_registry.infrastructure.s3_derived_snapshots import S3DerivedSnapshotRepository
from ifx_registry.infrastructure.s3_external_versions import S3ExternalDatasetVersionRepository
from ifx_registry.infrastructure.s3_snapshots import S3PublishedSnapshotRepository
from ifx_registry.infrastructure.scheduler import (
    ThreadAcquisitionScheduler,
    ThreadDerivedBuildScheduler,
)
from ifx_registry.infrastructure.version_check_store import SQLiteSourceVersionCheckStore
from ifx_registry.presentation.web.app import WebServices, WebSettings, create_app

from ...fakes import FakeObjectStore


class WebExampleSource(SourceAdapter):
    _dataset = DatasetId("example", "records")

    def __init__(self, latest_version: str = "2026-09") -> None:
        self.latest_version = latest_version
        self.fail_version_check = False

    @property
    def dataset(self) -> DatasetId:
        return self._dataset

    @property
    def homepage(self) -> str:
        return "https://example.org/records"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return ("https://example.org/records.tsv",)

    @property
    def version_check_description(self) -> str:
        return "Reads the release identifier from the example metadata endpoint."

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return ("https://example.org/version",)

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        if self.fail_version_check:
            raise SourceValidationError("Example release metadata is unavailable")
        return SourceVersion(self.latest_version)

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        request.progress.report(ProgressUpdate("downloading", "Downloading records", 1, 1))
        path = request.destination / "example" / "records" / self.latest_version / "records.tsv"
        path.parent.mkdir(parents=True)
        path.write_text("id\n1\n")
        return SourceSnapshot(
            dataset=self.dataset,
            version=SourceVersion(self.latest_version),
            files=(
                SnapshotFile(
                    path,
                    PurePosixPath("records.tsv"),
                    "https://example.org/records.tsv",
                ),
            ),
            downloaded_at=datetime.now(UTC),
        )


class PreviewRecipe(DerivedRecipe):
    @property
    def descriptor(self) -> DerivedRecipeDescriptor:
        return DerivedRecipeDescriptor(
            dataset=DatasetId("example", "derived_records"),
            display_name="Derived records",
            description="Builds from one exact records snapshot.",
            revision="1",
            inputs=(
                RecipeInputSlot(
                    "records",
                    "Records",
                    SnapshotKind.SOURCE,
                    DatasetId("example", "records"),
                ),
            ),
            producer=ProducerIdentity(
                "registry",
                "1",
                "https://example.org/registry",
                "a" * 40,
            ),
            transform={"name": "preview_fixture", "version": 1},
        )

    def build(self, inputs, destination, progress):  # type: ignore[no-untyped-def]
        raise AssertionError("PreviewRecipe is not executed")


def test_web_entrypoint_preserves_version_check_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture_app(settings: WebSettings) -> object:
        captured["settings"] = settings
        return object()

    def capture_run(application: object, *, host: str, port: int) -> None:
        captured["application"] = application
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setenv("IFX_REGISTRY_VERSION_CHECK_TTL_SECONDS", "12345")
    monkeypatch.setattr(sys, "argv", ["ifx-registry-web"])
    monkeypatch.setattr(web_app_module, "create_app", capture_app)
    monkeypatch.setattr("ifx_registry.presentation.web.app.uvicorn.run", capture_run)

    web_app_module.main()

    settings = captured["settings"]
    assert isinstance(settings, WebSettings)
    assert settings.version_check_ttl_seconds == 12345


def test_display_time_uses_configured_timezone() -> None:
    timestamp = datetime(2026, 7, 1, 18, tzinfo=UTC)

    assert web_app_module._format_datetime(
        timestamp, ZoneInfo("America/New_York")
    ) == "Jul 1, 2026 · 2:00\N{NO-BREAK SPACE}PM EDT"


def test_web_settings_reject_unknown_display_timezone(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown display timezone"):
        WebSettings(tmp_path, display_timezone="Not/A_Timezone")


def _services(
    tmp_path: Path,
    source: WebExampleSource | None = None,
    objects: FakeObjectStore | None = None,
) -> WebServices:
    source = source or WebExampleSource()
    catalog = InMemorySourceCatalog(
        (
            (
                SourceDescriptor(
                    dataset=source.dataset,
                    display_name="Example Records",
                    description="Example source",
                    expected_file_count=1,
                    homepage=source.homepage,
                    upstream_urls=source.upstream_urls,
                    version_check_description=source.version_check_description,
                    version_evidence_urls=source.version_evidence_urls,
                ),
                source,
            ),
        )
    )
    jobs = SQLiteAcquisitionJobStore(tmp_path / "registry.sqlite3")
    checks = SQLiteSourceVersionCheckStore(tmp_path / "registry.sqlite3")
    object_store = objects or FakeObjectStore()
    snapshots = S3PublishedSnapshotRepository(object_store)
    derived = S3DerivedSnapshotRepository(object_store)
    external = S3ExternalDatasetVersionRepository(object_store)
    acquire = AcquireSourceSnapshot(
        catalog,
        jobs,
        snapshots,
        LocalAcquisitionWorkspaceProvider(tmp_path / "work"),
    )
    scheduler = ThreadAcquisitionScheduler(acquire.execute)
    recipes = InMemoryDerivedRecipeCatalog(())
    planner = PlanDerivedBuild(recipes, snapshots, derived, external)
    derived_jobs = SQLiteDerivedBuildJobStore(tmp_path / "registry.sqlite3")
    derived_scheduler = ThreadDerivedBuildScheduler(lambda _job_id: None)
    browse_catalog = BrowsePublishedCatalog(snapshots, catalog)
    browse_registry = BrowseRegistryCatalog(snapshots, derived, external, catalog)
    return WebServices(
        list_sources=ListSources(catalog, jobs),
        check_source=CheckSourceVersion(catalog, checks),
        assess_source_update=AssessSourceUpdate(),
        start_acquisition=StartSourceAcquisition(
            catalog,
            jobs,
            snapshots,
            scheduler,
            checks,
        ),
        acquisition_queries=AcquisitionQueries(jobs),
        browse_catalog=browse_catalog,
        get_published_dataset=GetPublishedDataset(browse_catalog),
        get_published_snapshot=GetPublishedSnapshot(snapshots),
        browse_registry_catalog=browse_registry,
        get_registry_dataset=GetRegistryDataset(snapshots, derived, external),
        get_registry_version=GetRegistryVersion(snapshots, derived, external),
        get_registry_dataset_details=GetRegistryDatasetDetails(
            snapshots,
            derived,
            external,
            recipes,
        ),
        get_derived_build_options=GetDerivedBuildOptions(
            recipes,
            derived_jobs,
            snapshots,
            derived,
            external,
            planner,
        ),
        plan_derived_build=planner,
        start_derived_build=StartDerivedBuild(
            planner,
            derived_jobs,
            derived_scheduler,
            derived,
        ),
        derived_build_queries=DerivedBuildQueries(derived_jobs),
        derived_recipes=recipes,
        list_source_check_statuses=ListSourceCheckStatuses(checks),
        scheduler=scheduler,
        derived_scheduler=derived_scheduler,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_catalog_separates_configured_sources_from_registered_datasets(
    tmp_path: Path,
) -> None:
    app = create_app(WebSettings(tmp_path), _services(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            response = await client.get("/")

    assert response.status_code == 200
    assert "Datasets" in response.text
    assert "No datasets have been registered yet" in response.text
    assert "Sources available to register" in response.text
    assert "Example Records" in response.text
    assert "Nothing legacy is implied here" not in response.text
    assert 'href="/operations"' not in response.text
    assert 'id="registration-panel"' in response.text
    assert 'id="catalog-job-list"' in response.text


@pytest.mark.anyio
async def test_operations_redirects_to_catalog(
    tmp_path: Path,
) -> None:
    app = create_app(WebSettings(tmp_path), _services(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            response = await client.get("/operations", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.anyio
async def test_catalog_renders_queued_job_as_waiting_not_refreshable(tmp_path: Path) -> None:
    objects = FakeObjectStore()
    registered_file = tmp_path / "records.tsv"
    registered_file.write_text("id\n1\n")
    S3PublishedSnapshotRepository(objects).publish(
        SourceSnapshot(
            dataset=DatasetId("example", "records"),
            version=SourceVersion("2026-08"),
            files=(
                SnapshotFile(
                    registered_file,
                    PurePosixPath("records.tsv"),
                    "https://example.org/records.tsv",
                ),
            ),
        )
    )
    services = _services(tmp_path, objects=objects)
    SQLiteAcquisitionJobStore(tmp_path / "registry.sqlite3").add(
        AcquisitionJob(
            "queued-job",
            DatasetId("example", "records"),
            "2026-09",
        )
    )
    app = create_app(WebSettings(tmp_path), services)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            response = await client.get("/")

    assert response.status_code == 200
    assert 'aria-label="Waiting to start"' in response.text
    assert "Check for updates" not in response.text


@pytest.mark.anyio
async def test_version_check_and_acquisition_work_through_ui(tmp_path: Path) -> None:
    source = WebExampleSource()
    app = create_app(WebSettings(tmp_path), _services(tmp_path, source))
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            check = await client.post("/sources/example/records/check")
            assert check.status_code == 200
            assert '<tr class="source-row"' in check.text
            assert "2026-09" in check.text
            assert "Download &amp; register" in check.text

            checked_catalog = await client.get("/")
            assert "2026-09" in checked_catalog.text
            assert "Download &amp; register" in checked_catalog.text

            started = await client.post(
                "/datasets/example/records/acquisitions",
                data={"expected_version": "2026-09"},
            )
            assert started.status_code == 202
            match = re.search(r'id="job-([a-f0-9]+)"', started.text)
            assert match is not None

            job_id = match.group(1)
            for _attempt in range(40):
                status = await client.get(f"/jobs/{job_id}")
                if "succeeded" in status.text:
                    break
                await anyio.sleep(0.025)
            assert "succeeded" in status.text
            assert "View registered dataset" in status.text
            assert "Processed in" in status.text

            versions = await client.get("/datasets/source/example/records")
            assert versions.status_code == 200
            assert "Registered versions" in versions.text
            assert "About this dataset" in versions.text
            assert "Example source" in versions.text
            assert "Source website" in versions.text
            assert "How version checking works" in versions.text
            assert "Estimated refresh commitment" in versions.text
            assert "based on the current registered release" in versions.text
            assert "Last refresh was ready in" in versions.text
            assert "Reads the release identifier" in versions.text
            assert "Check for updates" in versions.text

            catalog = await client.get("/")
            assert "Status" in catalog.text
            assert "Refresh typically" in catalog.text
            assert 'aria-label="Up to date"' in catalog.text
            assert "no new check is needed until" in catalog.text

            catalog_check = await client.post("/datasets/example/records/catalog-check")
            assert catalog_check.status_code == 200
            assert "Up to date" in catalog_check.text
            assert 'aria-label="Up to date"' in catalog_check.text

            cached_catalog = await client.get("/")
            assert 'aria-label="Up to date"' in cached_catalog.text
            assert "no new check is needed until" in cached_catalog.text

            source.latest_version = "2026-10"
            dataset_check = await client.post("/datasets/example/records/check")
            assert dataset_check.status_code == 200
            assert "Unregistered version available" in dataset_check.text
            assert "Download &amp; register 2026-10" in dataset_check.text
            assert "Checked" in dataset_check.text

            source.fail_version_check = True
            failed_check = await client.post("/datasets/example/records/check")
            assert failed_check.status_code == 502
            assert "Couldn’t check upstream" in failed_check.text
            assert "Latest registered" in failed_check.text
            assert "2026-09" in failed_check.text

            snapshot = await client.get("/datasets/source/example/records/2026-09")
            assert snapshot.status_code == 200
            assert "records.tsv" in snapshot.text


@pytest.mark.anyio
async def test_catalog_rendering_preserves_names_and_escapes_file_breaks(
    tmp_path: Path,
) -> None:
    objects = FakeObjectStore()
    local_file = tmp_path / "unsafe_<tag>_file.tsv"
    local_file.write_text("id\n1\n")
    snapshots = S3PublishedSnapshotRepository(objects)
    snapshots.publish(
        SourceSnapshot(
            dataset=DatasetId("bioplex", "ppi"),
            version=SourceVersion("3.0"),
            files=(
                SnapshotFile(
                    local_file,
                    PurePosixPath("unsafe_<tag>_file.tsv"),
                    "https://example.org/unsafe-file.tsv",
                ),
            ),
            downloaded_at=datetime.now(UTC),
            homepage="javascript:alert(1)",
            upstream_urls=("javascript:alert(2)",),
        )
    )
    app = create_app(WebSettings(tmp_path), _services(tmp_path, objects=objects))
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            catalog = await client.get("/")
            dataset = await client.get("/datasets/source/bioplex/ppi")
            version = await client.get("/datasets/source/bioplex/ppi/3.0")

    assert catalog.status_code == 200
    assert "BioPlex" in catalog.text
    assert "PPI" in catalog.text
    assert 'data-kind="source"' in catalog.text
    assert "BioPlex PPI bioplex:ppi source catalog only 3.0" in catalog.text
    assert "Latest version" in catalog.text
    assert "Contents" in catalog.text
    assert "1 file · 5 B" in catalog.text
    assert "Catalog only" in catalog.text
    assert "version checking and registration are unavailable" in catalog.text
    assert dataset.status_code == 200
    assert "Automatic version checks and registration are not available" in dataset.text
    assert "Dependency tree" not in dataset.text
    assert "Dependency tree" not in dataset.text
    assert 'href="javascript:' not in dataset.text
    assert "javascript:alert(1)" in dataset.text
    assert version.status_code == 200
    assert "Dependency tree" not in version.text
    assert "unsafe_<wbr>&lt;tag&gt;_<wbr>file.tsv" in version.text
    assert "unsafe_<tag>_file.tsv" not in version.text
    assert "SHA-256" in version.text
    assert "Source provenance" in version.text
    assert "Dependency tree" not in version.text
    assert 'href="javascript:' not in version.text


@pytest.mark.anyio
async def test_health_endpoint(tmp_path: Path) -> None:
    app = create_app(WebSettings(tmp_path), _services(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            response = await client.get("/health")

    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_operational_state_outages_return_service_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _services(tmp_path)

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OperationalStateUnavailableError("Operational state unavailable")

    monkeypatch.setattr(services.start_acquisition, "execute", unavailable)
    monkeypatch.setattr(services.acquisition_queries, "get_job", unavailable)
    app = create_app(WebSettings(tmp_path), services)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            acquisition = await client.post(
                "/datasets/example/records/acquisitions",
                data={"expected_version": "2026-09"},
            )
            job = await client.get("/jobs/unavailable")

    assert acquisition.status_code == 503
    assert job.status_code == 503


@pytest.mark.anyio
async def test_catalog_browsing_survives_derived_activity_state_outage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _services(tmp_path)

    def unavailable() -> None:
        raise OperationalStateUnavailableError("Derived activity unavailable")

    monkeypatch.setattr(services.derived_build_queries, "list_attention", unavailable)
    app = create_app(WebSettings(tmp_path), services)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/")

    assert response.status_code == 200
    assert "Datasets" in response.text


@pytest.mark.anyio
async def test_unknown_direct_build_request_returns_a_stable_error_partial(
    tmp_path: Path,
) -> None:
    app = create_app(WebSettings(tmp_path), _services(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            response = await client.post(
                "/datasets/derived/unknown/result/builds",
                data={"output_version": "1"},
            )

    assert response.status_code == 404
    assert 'id="derived-build-control"' in response.text
    assert "No Registry build recipe is installed" in response.text


@pytest.mark.anyio
async def test_cross_origin_publication_request_is_blocked(tmp_path: Path) -> None:
    app = create_app(WebSettings(tmp_path), _services(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Origin": "https://attacker.example"},
        ) as client:
            response = await client.post("/sources/example/records/check")

    assert response.status_code == 403


@pytest.mark.anyio
async def test_readiness_checks_external_and_derived_catalogs(tmp_path: Path) -> None:
    objects = FakeObjectStore()
    objects.objects["external/example/database/1/manifest.yaml"] = b"not: [valid"
    app = create_app(WebSettings(tmp_path), _services(tmp_path, objects=objects))
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


@pytest.mark.anyio
async def test_unregistered_recipe_is_discoverable_and_has_a_build_page(
    tmp_path: Path,
) -> None:
    objects = FakeObjectStore()
    services = _services(tmp_path, objects=objects)
    recipes = InMemoryDerivedRecipeCatalog((PubchemCidMolecularInfoRecipe(),))
    sources = S3PublishedSnapshotRepository(objects)
    derived = S3DerivedSnapshotRepository(objects)
    external = S3ExternalDatasetVersionRepository(objects)
    derived_jobs = SQLiteDerivedBuildJobStore(tmp_path / "registry.sqlite3")
    planner = PlanDerivedBuild(recipes, sources, derived, external)
    services = replace(
        services,
        derived_recipes=recipes,
        derived_build_queries=DerivedBuildQueries(derived_jobs),
        get_derived_build_options=GetDerivedBuildOptions(
            recipes,
            derived_jobs,
            sources,
            derived,
            external,
            planner,
        ),
        plan_derived_build=planner,
        start_derived_build=StartDerivedBuild(
            planner,
            derived_jobs,
            services.derived_scheduler,
            derived,
        ),
    )
    app = create_app(WebSettings(tmp_path), services)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            catalog = await client.get("/")
            recipe_page = await client.get(
                "/datasets/derived/pubchem/cid_molecular_info"
            )

    assert catalog.status_code == 200
    assert "Derived datasets available to build" in catalog.text
    assert 'href="/datasets/derived/pubchem/cid_molecular_info"' in catalog.text
    assert recipe_page.status_code == 200
    assert "No registered versions yet" in recipe_page.text
    assert "Build and register a new version" in recipe_page.text
    assert "No registered versions available" in recipe_page.text
    assert 'name="output_version"' not in recipe_page.text


@pytest.mark.anyio
async def test_derived_build_preview_tracks_inputs_and_server_owns_version(
    tmp_path: Path,
) -> None:
    objects = FakeObjectStore()
    sources = S3PublishedSnapshotRepository(objects)
    for version in ("1", "2"):
        source_file = tmp_path / f"records-{version}.tsv"
        source_file.write_text(f"id\n{version}\n", encoding="utf-8")
        sources.publish(
            SourceSnapshot(
                DatasetId("example", "records"),
                SourceVersion(version),
                (SnapshotFile(source_file, PurePosixPath("records.tsv"), None),),
            )
        )

    services = _services(tmp_path, objects=objects)
    derived = S3DerivedSnapshotRepository(objects)
    external = S3ExternalDatasetVersionRepository(objects)
    recipes = InMemoryDerivedRecipeCatalog((PreviewRecipe(),))
    jobs = SQLiteDerivedBuildJobStore(tmp_path / "registry.sqlite3")
    planner = PlanDerivedBuild(recipes, sources, derived, external)
    services = replace(
        services,
        derived_recipes=recipes,
        get_derived_build_options=GetDerivedBuildOptions(
            recipes,
            jobs,
            sources,
            derived,
            external,
            planner,
        ),
        plan_derived_build=planner,
        start_derived_build=StartDerivedBuild(
            planner,
            jobs,
            services.derived_scheduler,
            derived,
        ),
        derived_build_queries=DerivedBuildQueries(jobs),
    )
    app = create_app(WebSettings(tmp_path), services)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Origin": "http://test"},
        ) as client:
            page = await client.get("/datasets/derived/example/derived_records")
            preview = await client.post(
                "/datasets/derived/example/derived_records/build-preview",
                data={"input.records": "1"},
            )
            start = await client.post(
                "/datasets/derived/example/derived_records/builds",
                data={"input.records": "1", "output_version": "tampered"},
            )

    default_version = re.search(r"deps-[0-9a-f]{12}", page.text)
    selected_version = re.search(r"deps-[0-9a-f]{12}", preview.text)
    assert page.status_code == 200
    assert preview.status_code == 200
    assert default_version is not None
    assert selected_version is not None
    assert default_version.group() != selected_version.group()
    assert "example:derived_records:" + selected_version.group() in preview.text
    assert start.status_code == 202
    assert selected_version.group() in start.text
    assert "tampered" not in start.text


@pytest.mark.anyio
async def test_derived_version_handles_dependency_comparison_failure(tmp_path: Path) -> None:
    class DetailsQuery:
        def execute(self, *args):  # type: ignore[no-untyped-def]
            raise RegistryUnavailableError("dependency catalog unavailable")

    services = replace(
        _services(tmp_path),
        get_registry_dataset_details=DetailsQuery(),  # type: ignore[arg-type]
    )
    app = create_app(WebSettings(tmp_path), services)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/datasets/derived/derived/records/1")

    assert response.status_code == 503
    assert "dependency catalog unavailable" in response.text


@pytest.mark.anyio
async def test_catalog_and_details_distinguish_source_derived_and_external(
    tmp_path: Path,
) -> None:
    objects = FakeObjectStore()
    source_file = tmp_path / "source.tsv"
    source_file.write_text("id\n1\n", encoding="utf-8")
    source_repository = S3PublishedSnapshotRepository(objects)
    source = source_repository.publish(
        SourceSnapshot(
            dataset=DatasetId("example", "records"),
            version=SourceVersion("1"),
            files=(
                SnapshotFile(
                    source_file,
                    PurePosixPath("source.tsv"),
                    "https://example.org/source.tsv",
                ),
            ),
        )
    )
    external = S3ExternalDatasetVersionRepository(objects).publish(
        ExternalDatasetVersion(
            dataset=DatasetId("chembl", "activity_database"),
            version=DatasetVersion("chembl36"),
            interface="mysql",
            access_mode="query",
            service_name="ChEMBL activity database",
            observed_at=datetime(2026, 9, 10, tzinfo=UTC),
            documentation_url="https://www.ebi.ac.uk/chembl/",
            version_evidence={"release": "chembl36"},
        )
    )
    derived_file = tmp_path / "derived.tsv"
    derived_file.write_text("id\n1\n", encoding="utf-8")
    S3DerivedSnapshotRepository(objects).publish(
        DerivedSnapshot(
            dataset=DatasetId("example", "records"),
            version=DatasetVersion("1"),
            files=(DerivedSnapshotFile(derived_file, PurePosixPath("derived.tsv")),),
            inputs=(
                SnapshotRef.source("example:records:1"),
                SnapshotRef.external("chembl:activity_database:chembl36"),
            ),
            producer=ProducerIdentity(
                "ifx_odin",
                "1",
                "https://github.com/ncats/IFX_ODIN",
                "a" * 40,
            ),
            transform={"name": "example_transform", "version": "1"},
            validation={"rows": 1},
            metadata={
                "service_observations": [
                    {
                        "service_id": "pubchem:pug_rest",
                        "service_name": "PubChem PUG REST",
                        "interface": "https",
                        "operation": "compound records by CID",
                        "endpoint_template": "https://example.org/{cids}",
                        "first_observed_at": "2026-09-10T12:00:00+00:00",
                        "last_observed_at": "2026-09-10T12:01:00+00:00",
                        "request_count": 3,
                        "retry_count": 1,
                        "http_status_counts": {"200": 2, "503": 1},
                        "worst_throttle": "yellow",
                        "response_payload_sha256": "c" * 64,
                    }
                ]
            },
        ),
        (
            RegisteredSnapshotRef(
                SnapshotRef.source("example:records:1"),
                source.manifest_uri,
                source.manifest_sha256,
            ),
            RegisteredSnapshotRef(
                SnapshotRef.external("chembl:activity_database:chembl36"),
                external.manifest_uri,
                external.manifest_sha256,
            ),
        ),
    )
    source_repository.publish(
        SourceSnapshot(
            dataset=DatasetId("example", "records"),
            version=SourceVersion("2"),
            files=(
                SnapshotFile(
                    source_file,
                    PurePosixPath("source.tsv"),
                    "https://example.org/source.tsv",
                ),
            ),
        )
    )
    app = create_app(WebSettings(tmp_path), _services(tmp_path, objects=objects))
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            catalog = await client.get("/")
            source_detail = await client.get("/datasets/source/example/records/1")
            derived_overview = await client.get("/datasets/derived/example/records")
            derived_detail = await client.get("/datasets/derived/example/records/1")
            external_detail = await client.get(
                "/datasets/external/chembl/activity_database/chembl36"
            )

    assert catalog.status_code == 200
    assert 'data-kind-filter="derived"' in catalog.text
    assert 'href="/datasets/source/example/records"' in catalog.text
    assert 'href="/datasets/derived/example/records"' in catalog.text
    assert "Metadata only" in catalog.text
    assert 'aria-label="Rebuild recommended"' in catalog.text
    assert "dependency-preview" not in catalog.text
    assert "1 latest registered · 1 older" not in catalog.text
    assert source_detail.status_code == 200
    assert "Source provenance" in source_detail.text
    assert "Dependency tree" in source_detail.text
    assert "Root datasets" in source_detail.text
    assert "Used by" in source_detail.text
    assert "Current details" in source_detail.text
    assert 'aria-label="View Example Records dataset"' in source_detail.text
    assert 'aria-label="View exact version 1 of Example Records" aria-current="page"' in (
        source_detail.text
    )
    assert len(re.findall(r"data-lineage-node", source_detail.text)) == 3
    assert "data-lineage-connectors" in source_detail.text
    assert "lineage-mobile-links" in source_detail.text
    assert "latest: <code>2</code>" in source_detail.text
    assert 'href="/datasets/source/example/records/2">Latest' not in source_detail.text
    assert derived_overview.status_code == 200
    assert "Build inputs by version" not in derived_overview.text
    assert "data-lineage-connectors" in derived_overview.text
    assert "Selected version" in derived_overview.text
    assert 'href="/datasets/derived/example/records"' in derived_overview.text
    assert (
        'href="/datasets/derived/example/records" '
        'aria-label="View Example Records dataset" aria-current="page"'
        in derived_overview.text
    )
    assert "A newer registered version exists somewhere" in derived_overview.text
    assert derived_detail.status_code == 200
    assert "Dependency tree" in derived_detail.text
    assert len(re.findall(r"data-lineage-node", derived_detail.text)) == 3
    assert "Built from" in derived_detail.text
    assert "2 inputs" in derived_detail.text
    assert "Older" in derived_detail.text
    assert "latest: <code>2</code>" in derived_detail.text
    assert "Metadata only" in derived_detail.text
    assert "Service observation" in derived_detail.text
    assert "Services queried during build" in derived_detail.text
    assert "PubChem PUG REST" in derived_detail.text
    assert "3 requests" in derived_detail.text
    assert "Stored-response digest" in derived_detail.text
    assert "example_transform" in derived_detail.text
    assert "ifx_odin" in derived_detail.text
    assert external_detail.status_code == 200
    assert "Dependency tree" in external_detail.text
    assert "Used by" in external_detail.text
    assert len(re.findall(r"data-lineage-node", external_detail.text)) == 3
    assert "The Registry records this version but does not store its data or credentials" in (
        external_detail.text
    )
    assert "ChEMBL activity database" in external_detail.text
    assert "<h2 id=\"files-title\">Files</h2>" not in external_detail.text
