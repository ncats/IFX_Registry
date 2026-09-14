"""Composition root and routes for the standalone Registry web application."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from ifx_registry.application.derived_build_models import SelectedRecipeInput
from ifx_registry.application.ports.derived_builds import DerivedRecipeCatalog
from ifx_registry.application.use_cases.acquire_source import (
    AcquireSourceSnapshot,
    StartSourceAcquisition,
)
from ifx_registry.application.use_cases.acquisition_queries import AcquisitionQueries
from ifx_registry.application.use_cases.assess_source_update import (
    AssessSourceUpdate,
    SourceUpdateAssessment,
)
from ifx_registry.application.use_cases.browse_catalog import (
    BrowsePublishedCatalog,
    BrowseRegistryCatalog,
    CatalogDataset,
    CatalogKind,
    GetPublishedDataset,
    GetPublishedSnapshot,
    GetRegistryDataset,
    GetRegistryVersion,
    PublishedDataset,
)
from ifx_registry.application.use_cases.build_derived_dataset import (
    BuildDerivedDataset,
    PlanDerivedBuild,
    StartDerivedBuild,
)
from ifx_registry.application.use_cases.check_source_version import CheckSourceVersion
from ifx_registry.application.use_cases.dataset_lineage import GetRegistryDatasetDetails
from ifx_registry.application.use_cases.derived_build_options import (
    DerivedBuildOptions,
    GetDerivedBuildOptions,
)
from ifx_registry.application.use_cases.derived_build_queries import DerivedBuildQueries
from ifx_registry.application.use_cases.list_sources import ListSources, SourceOverview
from ifx_registry.application.use_cases.source_check_status import (
    ListSourceCheckStatuses,
    SourceCheckStatus,
    VersionCheckPolicy,
)
from ifx_registry.domain.errors import (
    AcquisitionJobNotFoundError,
    CatalogConsistencyError,
    InvalidDatasetIdError,
    OperationalStateUnavailableError,
    RegistryError,
    RegistryUnavailableError,
    SnapshotNotFoundError,
)
from ifx_registry.domain.models import DatasetId, DatasetVersion, SnapshotRef, SourceVersion
from ifx_registry.infrastructure.aws_credentials import (
    AwsCredentialSettings,
    load_aws_credentials,
)
from ifx_registry.infrastructure.cure_credentials import load_cure_credentials
from ifx_registry.infrastructure.derived_build_job_store import SQLiteDerivedBuildJobStore
from ifx_registry.infrastructure.derived_recipe_catalog import InMemoryDerivedRecipeCatalog
from ifx_registry.infrastructure.http import RequestsHttpGateway
from ifx_registry.infrastructure.job_store import SQLiteAcquisitionJobStore
from ifx_registry.infrastructure.local_workspaces import LocalAcquisitionWorkspaceProvider
from ifx_registry.infrastructure.materialization import FileSystemSnapshotMaterializer
from ifx_registry.infrastructure.object_store import Boto3ObjectStore
from ifx_registry.infrastructure.recipes import built_in_recipes
from ifx_registry.infrastructure.s3_derived_snapshots import S3DerivedSnapshotRepository
from ifx_registry.infrastructure.s3_external_versions import S3ExternalDatasetVersionRepository
from ifx_registry.infrastructure.s3_snapshots import S3PublishedSnapshotRepository
from ifx_registry.infrastructure.scheduler import (
    ThreadAcquisitionScheduler,
    ThreadDerivedBuildScheduler,
)
from ifx_registry.infrastructure.source_configuration import (
    DEFAULT_SOURCE_CONFIGURATION,
    YamlSourceCatalogLoader,
)
from ifx_registry.infrastructure.source_factory import BuiltInSourceFactory
from ifx_registry.infrastructure.version_check_store import SQLiteSourceVersionCheckStore

_WEB_ROOT = Path(__file__).parent

_SOURCE_DISPLAY_NAMES = {
    "antibodypedia": "Antibodypedia",
    "bioplex": "BioPlex",
    "chebi": "ChEBI",
    "chembl": "ChEMBL",
    "ctd": "CTD",
    "cure": "CURE ID",
    "dark_kinome": "Dark Kinome",
    "expasy": "ExPASy",
    "glygen": "GlyGen",
    "go": "Gene Ontology",
    "gtex": "GTEx",
    "hcop": "HCOP",
    "hmdb": "HMDB",
    "hpa": "Human Protein Atlas",
    "hpm": "Human Proteome Map",
    "impc": "IMPC",
    "iuphar": "IUPHAR",
    "linkedomics": "LinkedOmics",
    "lipidmaps": "LIPID MAPS",
    "mgi": "MGI",
    "mondo": "MONDO",
    "mp": "Mammalian Phenotype Ontology",
    "ncbi": "NCBI",
    "panther": "PANTHER",
    "pathwaycommons": "Pathway Commons",
    "pfocr": "PFOCR",
    "pubchem": "PubChem",
    "pubtator": "PubTator",
    "ramp": "RaMP",
    "refmet": "RefMet",
    "resolute": "RESOLUTE",
    "string": "STRING",
    "surechembl": "SureChEMBL",
    "tiga": "TIGA",
    "uberon": "UBERON",
    "uniprot": "UniProt",
    "wikipathways": "WikiPathways",
}

_DATASET_ACRONYMS = {
    "csv": "CSV",
    "go": "GO",
    "gmt": "GMT",
    "id": "ID",
    "ids": "IDs",
    "json": "JSON",
    "ppi": "PPI",
    "rdf": "RDF",
    "sdf": "SDF",
    "tsv": "TSV",
    "xml": "XML",
}


@dataclass(frozen=True, slots=True)
class WebSettings:
    state_directory: Path = Path("registry-state")
    source_configuration: Path = DEFAULT_SOURCE_CONFIGURATION
    host: str = "127.0.0.1"
    port: int = 8000
    root_path: str = ""
    bucket: str | None = None
    aws_region: str | None = None
    s3_prefix: str = ""
    s3_endpoint_url: str | None = None
    aws_role_arn: str | None = None
    aws_external_id: str | None = None
    aws_credentials: Path | None = None
    cure_credentials: Path | None = None
    display_timezone: str = "America/New_York"
    version_check_ttl_seconds: int = 7 * 24 * 60 * 60

    def __post_init__(self) -> None:
        normalized_root_path = self.root_path.rstrip("/")
        if normalized_root_path and not normalized_root_path.startswith("/"):
            raise ValueError("root path must be empty or start with '/'")
        object.__setattr__(self, "root_path", normalized_root_path)
        if self.version_check_ttl_seconds <= 0:
            raise ValueError("version-check TTL must be positive")
        try:
            ZoneInfo(self.display_timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(
                f"unknown display timezone: {self.display_timezone}"
            ) from error

    @classmethod
    def from_environment(cls) -> WebSettings:
        configured_sources = os.environ.get("IFX_REGISTRY_SOURCES_CONFIG")
        return cls(
            state_directory=Path(os.environ.get("IFX_REGISTRY_STATE_DIR", "registry-state")),
            source_configuration=(
                Path(configured_sources) if configured_sources else DEFAULT_SOURCE_CONFIGURATION
            ),
            host=os.environ.get("IFX_REGISTRY_HOST", "127.0.0.1"),
            port=int(os.environ.get("IFX_REGISTRY_PORT", "8000")),
            root_path=os.environ.get("IFX_REGISTRY_ROOT_PATH", ""),
            bucket=os.environ.get("IFX_REGISTRY_BUCKET"),
            aws_region=os.environ.get("AWS_REGION"),
            s3_prefix=os.environ.get("IFX_REGISTRY_S3_PREFIX", ""),
            s3_endpoint_url=os.environ.get("IFX_REGISTRY_S3_ENDPOINT_URL"),
            aws_role_arn=os.environ.get("IFX_REGISTRY_AWS_ROLE_ARN"),
            aws_external_id=os.environ.get("IFX_REGISTRY_AWS_EXTERNAL_ID"),
            aws_credentials=(
                Path(value) if (value := os.environ.get("IFX_REGISTRY_AWS_CREDENTIALS")) else None
            ),
            cure_credentials=(
                Path(value) if (value := os.environ.get("IFX_REGISTRY_CURE_CREDENTIALS")) else None
            ),
            display_timezone=os.environ.get(
                "IFX_REGISTRY_DISPLAY_TIMEZONE", "America/New_York"
            ),
            version_check_ttl_seconds=int(
                os.environ.get("IFX_REGISTRY_VERSION_CHECK_TTL_SECONDS", 7 * 24 * 60 * 60)
            ),
        )


@dataclass(frozen=True, slots=True)
class WebServices:
    list_sources: ListSources
    check_source: CheckSourceVersion
    assess_source_update: AssessSourceUpdate
    start_acquisition: StartSourceAcquisition
    acquisition_queries: AcquisitionQueries
    browse_catalog: BrowsePublishedCatalog
    get_published_snapshot: GetPublishedSnapshot
    get_published_dataset: GetPublishedDataset
    browse_registry_catalog: BrowseRegistryCatalog
    get_registry_dataset: GetRegistryDataset
    get_registry_version: GetRegistryVersion
    get_registry_dataset_details: GetRegistryDatasetDetails
    get_derived_build_options: GetDerivedBuildOptions
    plan_derived_build: PlanDerivedBuild
    start_derived_build: StartDerivedBuild
    derived_build_queries: DerivedBuildQueries
    derived_recipes: DerivedRecipeCatalog
    list_source_check_statuses: ListSourceCheckStatuses
    scheduler: ThreadAcquisitionScheduler
    derived_scheduler: ThreadDerivedBuildScheduler


@dataclass(frozen=True, slots=True)
class DatasetUpdateResult:
    overview: SourceOverview
    checked_version: SourceVersion | None
    assessment: SourceUpdateAssessment | None
    check_error: str | None
    registry_error: str | None
    status_code: int
    latest_registered_version: SourceVersion | None
    latest_registered_size_bytes: int | None

    def template_context(self) -> dict[str, object]:
        return {
            "overview": self.overview,
            "checked_version": self.checked_version,
            "assessment": self.assessment,
            "check_error": self.check_error,
            "registry_error": self.registry_error,
            "latest_registered_version": self.latest_registered_version,
            "latest_registered_size_bytes": self.latest_registered_size_bytes,
        }


def build_services(settings: WebSettings) -> WebServices:
    state_directory = settings.state_directory.resolve()
    http = RequestsHttpGateway()
    cure_api_key = (
        load_cure_credentials(settings.cure_credentials).api_key
        if settings.cure_credentials is not None
        else None
    )
    sources = YamlSourceCatalogLoader(
        BuiltInSourceFactory(http, cure_api_key=cure_api_key)
    ).load(
        settings.source_configuration
    )
    jobs = SQLiteAcquisitionJobStore(state_directory / "registry.sqlite3")
    derived_jobs = SQLiteDerivedBuildJobStore(state_directory / "registry.sqlite3")
    derived_jobs.recover_interrupted()
    checks = SQLiteSourceVersionCheckStore(state_directory / "registry.sqlite3")
    credentials: AwsCredentialSettings | None = None
    if settings.aws_credentials is not None:
        credentials = load_aws_credentials(settings.aws_credentials)
    bucket = settings.bucket or (credentials.bucket if credentials else "aws-ifx-registry")
    region = settings.aws_region or (credentials.region if credentials else "us-east-1")
    objects = Boto3ObjectStore(
        bucket,
        region=region,
        endpoint_url=(
            settings.s3_endpoint_url or (credentials.endpoint_url if credentials else None)
        ),
        role_arn=settings.aws_role_arn or (credentials.role_arn if credentials else None),
        external_id=(
            settings.aws_external_id or (credentials.external_id if credentials else None)
        ),
        role_session_name=credentials.session_name if credentials else "ifx-registry",
        access_key_id=credentials.access_key_id if credentials else None,
        secret_access_key=credentials.secret_access_key if credentials else None,
    )
    snapshots = S3PublishedSnapshotRepository(objects, prefix=settings.s3_prefix)
    derived = S3DerivedSnapshotRepository(objects, prefix=settings.s3_prefix)
    external = S3ExternalDatasetVersionRepository(objects, prefix=settings.s3_prefix)
    recipes = InMemoryDerivedRecipeCatalog(built_in_recipes())
    derived_planner = PlanDerivedBuild(recipes, snapshots, derived, external)
    workspaces = LocalAcquisitionWorkspaceProvider(state_directory / "work")
    workspaces.cleanup_abandoned()
    acquire = AcquireSourceSnapshot(sources, jobs, snapshots, workspaces)
    scheduler = ThreadAcquisitionScheduler(acquire.execute)
    materializer = FileSystemSnapshotMaterializer(snapshots)
    build_derived = BuildDerivedDataset(
        recipes,
        derived_jobs,
        snapshots,
        derived,
        external,
        materializer,
        derived,
        workspaces,
    )
    derived_scheduler = ThreadDerivedBuildScheduler(build_derived.execute)
    browse_catalog = BrowsePublishedCatalog(snapshots, sources)
    browse_registry_catalog = BrowseRegistryCatalog(snapshots, derived, external, sources)
    return WebServices(
        list_sources=ListSources(sources, jobs),
        check_source=CheckSourceVersion(sources, checks),
        assess_source_update=AssessSourceUpdate(),
        start_acquisition=StartSourceAcquisition(
            sources,
            jobs,
            snapshots,
            scheduler,
            checks,
            check_freshness=timedelta(seconds=settings.version_check_ttl_seconds),
        ),
        acquisition_queries=AcquisitionQueries(jobs),
        browse_catalog=browse_catalog,
        get_published_dataset=GetPublishedDataset(browse_catalog),
        get_published_snapshot=GetPublishedSnapshot(snapshots),
        browse_registry_catalog=browse_registry_catalog,
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
            derived_planner,
        ),
        plan_derived_build=derived_planner,
        start_derived_build=StartDerivedBuild(
            derived_planner,
            derived_jobs,
            derived_scheduler,
            derived,
        ),
        derived_build_queries=DerivedBuildQueries(derived_jobs),
        derived_recipes=recipes,
        list_source_check_statuses=ListSourceCheckStatuses(
            checks,
            VersionCheckPolicy(timedelta(seconds=settings.version_check_ttl_seconds)),
        ),
        scheduler=scheduler,
        derived_scheduler=derived_scheduler,
    )


def create_app(
    settings: WebSettings | None = None,
    services: WebServices | None = None,
) -> FastAPI:
    resolved_settings = settings or WebSettings.from_environment()
    resolved_services = services or build_services(resolved_settings)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        yield
        resolved_services.scheduler.shutdown()
        resolved_services.derived_scheduler.shutdown()

    application = FastAPI(
        title="IFX Data Registry",
        version="0.2.0",
        root_path=resolved_settings.root_path,
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=_WEB_ROOT / "templates")
    templates.env.globals["root_path"] = resolved_settings.root_path
    display_timezone = ZoneInfo(resolved_settings.display_timezone)
    templates.env.filters["displaytime"] = lambda value: _format_datetime(
        value, display_timezone
    )
    templates.env.filters["filesize"] = _format_file_size
    templates.env.filters["duration"] = _format_duration
    templates.env.filters["sourcename"] = _format_source_name
    templates.env.filters["datasetname"] = _format_dataset_name
    templates.env.filters["breakfilename"] = _break_filename
    templates.env.filters["safeurl"] = _safe_http_url
    templates.env.filters["jsonpretty"] = _format_json
    application.mount(
        "/static",
        StaticFiles(directory=_WEB_ROOT / "static"),
        name="static",
    )

    def find_source_overview(dataset: DatasetId) -> SourceOverview | None:
        return next(
            (
                item
                for item in resolved_services.list_sources.execute()
                if item.descriptor.dataset == dataset
            ),
            None,
        )

    def source_overview(dataset: DatasetId) -> SourceOverview:
        overview = find_source_overview(dataset)
        if overview is None:
            raise HTTPException(status_code=404, detail=f"Unknown Registry source: {dataset}")
        return overview

    def parse_dataset(source_name: str, dataset_name: str) -> DatasetId:
        try:
            dataset = DatasetId(source=source_name, dataset=dataset_name)
            source_overview(dataset)
            return dataset
        except (RegistryError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    def require_same_origin(request: Request) -> None:
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.headers.get("origin") != expected_origin:
            raise HTTPException(status_code=403, detail="Cross-origin operation blocked")

    def selected_recipe_inputs(
        form: Mapping[str, object],
        options: DerivedBuildOptions,
    ) -> tuple[SelectedRecipeInput, ...]:
        selected = []
        for input_options in options.inputs:
            version = DatasetVersion(
                str(form.get(f"input.{input_options.slot.name}", ""))
            )
            selected.append(
                SelectedRecipeInput(
                    input_options.slot.name,
                    SnapshotRef(
                        input_options.slot.kind,
                        input_options.slot.dataset,
                        version,
                    ),
                )
            )
        return tuple(selected)

    def check_update(dataset: DatasetId) -> DatasetUpdateResult:
        overview = source_overview(dataset)
        checked_version: SourceVersion | None = None
        check_error: str | None = None
        registry_error: str | None = None
        registry_error_status: int | None = None
        registered_dataset: PublishedDataset | None = None
        try:
            registered_dataset = resolved_services.get_published_dataset.execute(dataset)
        except SnapshotNotFoundError:
            pass
        except RegistryUnavailableError as error:
            registry_error = str(error)
            registry_error_status = 503
        except CatalogConsistencyError as error:
            registry_error = str(error)
            registry_error_status = 500
        try:
            checked_version = resolved_services.check_source.execute(dataset)
        except OperationalStateUnavailableError as error:
            registry_error = str(error)
            registry_error_status = 503
        except RegistryError as error:
            check_error = str(error)
        assessment = None
        if checked_version is not None and registry_error is None:
            assessment = resolved_services.assess_source_update.execute(
                checked_version,
                registered_dataset,
            )
        status_code = registry_error_status or (502 if check_error is not None else 200)
        return DatasetUpdateResult(
            overview=overview,
            checked_version=checked_version,
            assessment=assessment,
            check_error=check_error,
            registry_error=registry_error,
            status_code=status_code,
            latest_registered_version=(
                registered_dataset.latest.version if registered_dataset else None
            ),
            latest_registered_size_bytes=(
                registered_dataset.latest.total_size_bytes if registered_dataset else None
            ),
        )

    @application.get("/", response_class=HTMLResponse)
    def catalog(request: Request) -> HTMLResponse:
        catalog_error: str | None = None
        datasets: tuple[CatalogDataset, ...] = ()
        overviews = resolved_services.list_sources.execute()
        registered_by_dataset: dict[DatasetId, PublishedDataset] = {}
        try:
            datasets = resolved_services.browse_registry_catalog.execute()
            registered_by_dataset = {
                item.dataset: item for item in resolved_services.browse_catalog.execute()
            }
        except RegistryError as error:
            catalog_error = str(error)
        registered = {
            item.dataset for item in datasets if item.kind is CatalogKind.SOURCE
        }
        source_overviews = {overview.descriptor.dataset: overview for overview in overviews}
        available_sources = (
            tuple(
                overview for overview in overviews if overview.descriptor.dataset not in registered
            )
            if catalog_error is None
            else ()
        )
        source_attention_jobs = tuple(
            job
            for overview in overviews
            if (
                job := overview.active_job
                or (
                    overview.latest_job
                    if overview.latest_job and overview.latest_job.status.value == "failed"
                    else None
                )
            )
        )
        try:
            derived_attention_jobs = resolved_services.derived_build_queries.list_attention()
        except OperationalStateUnavailableError:
            derived_attention_jobs = ()
        source_check_statuses: dict[DatasetId, SourceCheckStatus] = {}
        if any(overview.operational_state_available for overview in overviews):
            try:
                source_check_statuses = dict(
                    resolved_services.list_source_check_statuses.execute(
                        (overview.descriptor.dataset for overview in overviews),
                        registered_by_dataset,
                    )
                )
            except OperationalStateUnavailableError:
                pass
        return templates.TemplateResponse(
            request=request,
            name="catalog.html",
            context={
                "active_page": "catalog",
                "datasets": datasets,
                "catalog_error": catalog_error,
                "source_overviews": source_overviews,
                "available_sources": available_sources,
                "available_recipes": tuple(
                    recipe
                    for recipe in resolved_services.derived_recipes.list_descriptors()
                    if recipe.dataset
                    not in {
                        item.dataset
                        for item in datasets
                        if item.kind is CatalogKind.DERIVED
                    }
                ),
                "source_attention_jobs": source_attention_jobs,
                "derived_attention_jobs": derived_attention_jobs,
                "has_activity": bool(source_attention_jobs or derived_attention_jobs),
                "source_check_statuses": source_check_statuses,
            },
        )

    @application.get("/datasets/{source_name}/{dataset_name}")
    def legacy_dataset_versions(
        request: Request, source_name: str, dataset_name: str
    ) -> RedirectResponse:
        return RedirectResponse(
            url=(
                f"{request.scope.get('root_path', '')}/datasets/source/"
                f"{source_name}/{dataset_name}"
            ),
            status_code=308,
        )

    @application.get(
        "/datasets/{kind_name}/{source_name}/{dataset_name}",
        response_class=HTMLResponse,
    )
    def dataset_versions(
        request: Request,
        kind_name: str,
        source_name: str,
        dataset_name: str,
    ) -> Response:
        try:
            kind = CatalogKind(kind_name)
        except ValueError:
            return RedirectResponse(
                url=(
                    f"{request.scope.get('root_path', '')}/datasets/source/"
                    f"{kind_name}/{source_name}/{dataset_name}"
                ),
                status_code=308,
            )
        try:
            dataset = DatasetId(source_name, dataset_name)
            details = resolved_services.get_registry_dataset_details.execute(kind, dataset)
            published_dataset = details.dataset
        except SnapshotNotFoundError as error:
            if kind is CatalogKind.DERIVED:
                build_options_error = None
                try:
                    build_options = resolved_services.get_derived_build_options.execute(dataset)
                except OperationalStateUnavailableError as build_error:
                    build_options = None
                    build_options_error = str(build_error)
                recipe = None
                try:
                    recipe = resolved_services.derived_recipes.get_recipe(dataset).descriptor
                except RegistryError:
                    pass
                if build_options is not None or recipe is not None:
                    return templates.TemplateResponse(
                        request=request,
                        name="derived_recipe_dataset.html",
                        context={
                            "active_page": "catalog",
                            "recipe": build_options.recipe if build_options else recipe,
                            "build_options": build_options,
                            "build_options_error": build_options_error,
                        },
                    )
            return templates.TemplateResponse(
                request=request,
                name="not_found.html",
                context={"active_page": "catalog", "message": str(error)},
                status_code=404,
            )
        except (InvalidDatasetIdError, ValueError) as error:
            return templates.TemplateResponse(
                request=request,
                name="not_found.html",
                context={"active_page": "catalog", "message": str(error)},
                status_code=404,
            )
        except RegistryUnavailableError as error:
            return templates.TemplateResponse(
                request=request,
                name="not_found.html",
                context={"active_page": "catalog", "message": str(error)},
                status_code=503,
            )
        except CatalogConsistencyError as error:
            return templates.TemplateResponse(
                request=request,
                name="not_found.html",
                context={"active_page": "catalog", "message": str(error)},
                status_code=500,
            )
        build_options = None
        build_options_error = None
        if kind is CatalogKind.DERIVED:
            try:
                build_options = resolved_services.get_derived_build_options.execute(dataset)
            except OperationalStateUnavailableError as error:
                build_options_error = str(error)
        return templates.TemplateResponse(
            request=request,
            name="dataset.html",
            context={
                "active_page": "catalog",
                "item": published_dataset,
                "selected_snapshot": details.selected,
                "lineage_tree": details.lineage,
                "lineage_page": "overview",
                "build_options": build_options,
                "build_options_error": build_options_error,
                "overview": (
                    find_source_overview(published_dataset.dataset)
                    if kind is CatalogKind.SOURCE
                    else None
                ),
            },
        )

    @application.post(
        "/datasets/{source_name}/{dataset_name}/check",
        response_class=HTMLResponse,
    )
    def check_dataset_source(
        request: Request,
        source_name: str,
        dataset_name: str,
    ) -> HTMLResponse:
        require_same_origin(request)
        dataset = parse_dataset(source_name, dataset_name)
        result = check_update(dataset)
        return templates.TemplateResponse(
            request=request,
            name="partials/dataset_update.html",
            context=result.template_context(),
            status_code=result.status_code,
        )

    @application.post(
        "/datasets/{source_name}/{dataset_name}/catalog-check",
        response_class=HTMLResponse,
    )
    def check_catalog_source(
        request: Request,
        source_name: str,
        dataset_name: str,
    ) -> HTMLResponse:
        require_same_origin(request)
        dataset = parse_dataset(source_name, dataset_name)
        result = check_update(dataset)
        return templates.TemplateResponse(
            request=request,
            name="partials/catalog_update.html",
            context=result.template_context(),
            status_code=result.status_code,
        )

    @application.get(
        "/datasets/{kind_name}/{source_name}/{dataset_name}/{version}",
        response_class=HTMLResponse,
    )
    def dataset_version(
        request: Request,
        kind_name: str,
        source_name: str,
        dataset_name: str,
        version: str,
    ) -> HTMLResponse:
        try:
            kind = CatalogKind(kind_name)
            dataset = DatasetId(source_name, dataset_name)
            details = resolved_services.get_registry_dataset_details.execute(
                kind,
                dataset,
                version,
            )
            snapshot = details.selected
            lineage = details.dataset.lineage_for(snapshot.snapshot_id)
        except (SnapshotNotFoundError, InvalidDatasetIdError, ValueError) as error:
            return templates.TemplateResponse(
                request=request,
                name="not_found.html",
                context={"active_page": "catalog", "message": str(error)},
                status_code=404,
            )
        except RegistryUnavailableError as error:
            return templates.TemplateResponse(
                request=request,
                name="not_found.html",
                context={"active_page": "catalog", "message": str(error)},
                status_code=503,
            )
        except CatalogConsistencyError as error:
            return templates.TemplateResponse(
                request=request,
                name="not_found.html",
                context={"active_page": "catalog", "message": str(error)},
                status_code=500,
            )
        return templates.TemplateResponse(
            request=request,
            name="dataset_version.html",
            context={
                "active_page": "catalog",
                "snapshot": snapshot,
                "kind": kind,
                "item": details.dataset,
                "lineage": lineage,
                "lineage_tree": details.lineage,
                "lineage_page": "version",
                "build_options": None,
            },
        )

    @application.post(
        "/datasets/derived/{source_name}/{dataset_name}/build-preview",
        response_class=HTMLResponse,
    )
    async def preview_derived_build(
        request: Request,
        source_name: str,
        dataset_name: str,
    ) -> HTMLResponse:
        require_same_origin(request)
        options = None
        try:
            dataset = DatasetId(source_name, dataset_name)
            options = resolved_services.get_derived_build_options.execute(dataset)
            if options is None:
                raise SnapshotNotFoundError(
                    f"No Registry build recipe is installed for {dataset}"
                )
            form = await request.form()
            plan = resolved_services.plan_derived_build.execute(
                dataset,
                selected_recipe_inputs(form, options),
            )
            return templates.TemplateResponse(
                request=request,
                name="partials/derived_build_preview.html",
                context={
                    "build_options": options,
                    "output_version": plan.output_version,
                    "registered_output_versions": options.registered_output_versions,
                    "preview_error": None,
                },
            )
        except (OperationalStateUnavailableError, RegistryUnavailableError) as error:
            status_code = 503
            preview_error = str(error)
        except CatalogConsistencyError as error:
            status_code = 500
            preview_error = str(error)
        except (SnapshotNotFoundError, InvalidDatasetIdError) as error:
            status_code = 404
            preview_error = str(error)
        except (RegistryError, ValueError) as error:
            status_code = 422
            preview_error = str(error)
        return templates.TemplateResponse(
            request=request,
            name="partials/derived_build_preview.html",
            context={
                "build_options": options,
                "output_version": "",
                "registered_output_versions": frozenset(),
                "preview_error": preview_error,
            },
            status_code=status_code,
        )

    @application.post(
        "/datasets/derived/{source_name}/{dataset_name}/builds",
        response_class=HTMLResponse,
    )
    async def start_derived_build(
        request: Request,
        source_name: str,
        dataset_name: str,
    ) -> HTMLResponse:
        require_same_origin(request)
        submitted: dict[str, str] = {}
        options = None
        calculated_output_version = None
        try:
            dataset = DatasetId(source_name, dataset_name)
            options = resolved_services.get_derived_build_options.execute(dataset)
            if options is None:
                raise SnapshotNotFoundError(
                    f"No Registry build recipe is installed for {dataset}"
                )
            form = await request.form()
            selected = selected_recipe_inputs(form, options)
            for item in selected:
                submitted[item.slot] = item.reference.version.value
            calculated_output_version = resolved_services.plan_derived_build.execute(
                dataset,
                selected,
            ).output_version
            job = resolved_services.start_derived_build.execute(
                dataset,
                selected,
            )
        except (OperationalStateUnavailableError, RegistryUnavailableError) as error:
            return _derived_build_error(
                request,
                str(error),
                503,
                options,
                submitted,
                calculated_output_version,
                display_timezone,
            )
        except CatalogConsistencyError as error:
            return _derived_build_error(
                request,
                str(error),
                500,
                options,
                submitted,
                calculated_output_version,
                display_timezone,
            )
        except (SnapshotNotFoundError, InvalidDatasetIdError) as error:
            return _derived_build_error(
                request,
                str(error),
                404,
                options,
                submitted,
                calculated_output_version,
                display_timezone,
            )
        except RegistryError as error:
            return _derived_build_error(
                request,
                str(error),
                409,
                options,
                submitted,
                calculated_output_version,
                display_timezone,
            )
        except ValueError as error:
            return _derived_build_error(
                request,
                str(error),
                422,
                options,
                submitted,
                calculated_output_version,
                display_timezone,
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/derived_build_job.html",
            context={"job": job},
            status_code=202,
        )

    @application.get("/build-jobs/{job_id}", response_class=HTMLResponse)
    def derived_build_status(request: Request, job_id: str) -> HTMLResponse:
        try:
            job = resolved_services.derived_build_queries.get(job_id)
        except OperationalStateUnavailableError as error:
            return templates.TemplateResponse(
                request=request,
                name="partials/derived_build_error.html",
                context={"message": str(error)},
                status_code=503,
            )
        except RegistryError as error:
            return templates.TemplateResponse(
                request=request,
                name="partials/derived_build_error.html",
                context={"message": str(error)},
                status_code=404,
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/derived_build_job.html",
            context={"job": job},
        )

    @application.get("/operations", response_class=RedirectResponse)
    def operations(request: Request) -> RedirectResponse:
        return RedirectResponse(
            url=f"{request.scope.get('root_path', '')}/", status_code=303
        )

    @application.post(
        "/sources/{source_name}/{dataset_name}/check",
        response_class=HTMLResponse,
    )
    def check_source(request: Request, source_name: str, dataset_name: str) -> HTMLResponse:
        require_same_origin(request)
        dataset = parse_dataset(source_name, dataset_name)
        checked_version: SourceVersion | None = None
        check_error: str | None = None
        try:
            checked_version = resolved_services.check_source.execute(dataset)
        except OperationalStateUnavailableError as error:
            check_error = str(error)
            return templates.TemplateResponse(
                request=request,
                name="partials/source_row.html",
                context={
                    "overview": source_overview(dataset),
                    "checked_version": None,
                    "check_error": check_error,
                },
                status_code=503,
            )
        except RegistryError as error:
            check_error = str(error)
        return templates.TemplateResponse(
            request=request,
            name="partials/source_row.html",
            context={
                "overview": source_overview(dataset),
                "checked_version": checked_version,
                "check_error": check_error,
            },
            status_code=200 if check_error is None else 502,
        )

    @application.post(
        "/datasets/{source_name}/{dataset_name}/acquisitions",
        response_class=HTMLResponse,
    )
    def start_acquisition(
        request: Request,
        source_name: str,
        dataset_name: str,
        expected_version: Annotated[str, Form()],
    ) -> HTMLResponse:
        require_same_origin(request)
        dataset = parse_dataset(source_name, dataset_name)
        try:
            job = resolved_services.start_acquisition.execute(dataset, expected_version)
        except OperationalStateUnavailableError as error:
            return templates.TemplateResponse(
                request=request,
                name="partials/action_error.html",
                context={"message": str(error)},
                status_code=503,
            )
        except RegistryUnavailableError as error:
            return templates.TemplateResponse(
                request=request,
                name="partials/action_error.html",
                context={"message": str(error)},
                status_code=503,
            )
        except CatalogConsistencyError as error:
            return templates.TemplateResponse(
                request=request,
                name="partials/action_error.html",
                context={"message": str(error)},
                status_code=500,
            )
        except (RegistryError, ValueError) as error:
            return templates.TemplateResponse(
                request=request,
                name="partials/action_error.html",
                context={"message": str(error)},
                status_code=409,
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/job_card.html",
            context={"job": job},
            status_code=202,
        )

    @application.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_status(request: Request, job_id: str) -> HTMLResponse:
        try:
            job = resolved_services.acquisition_queries.get_job(job_id)
        except OperationalStateUnavailableError as error:
            return templates.TemplateResponse(
                request=request,
                name="partials/action_error.html",
                context={"message": str(error)},
                status_code=503,
            )
        except AcquisitionJobNotFoundError as error:
            return templates.TemplateResponse(
                request=request,
                name="partials/action_error.html",
                context={"message": str(error)},
                status_code=404,
            )
        return templates.TemplateResponse(
            request=request,
            name="partials/job_card.html",
            context={"job": job},
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    def readiness() -> JSONResponse:
        try:
            resolved_services.browse_registry_catalog.execute()
        except RegistryError as error:
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "detail": str(error)},
            )
        return JSONResponse(content={"status": "ready"})

    return application


def _derived_build_error(
    request: Request,
    message: str,
    status_code: int,
    options: DerivedBuildOptions | None,
    submitted: dict[str, str],
    calculated_output_version: str | None,
    display_timezone: ZoneInfo,
) -> HTMLResponse:
    templates = Jinja2Templates(directory=_WEB_ROOT / "templates")
    templates.env.filters["displaytime"] = lambda value: _format_datetime(
        value, display_timezone
    )
    if options is None:
        return templates.TemplateResponse(
            request=request,
            name="partials/derived_build_error.html",
            context={"message": message},
            status_code=status_code,
        )
    return templates.TemplateResponse(
        request=request,
        name="partials/derived_build_control.html",
        context={
            "build_options": options,
            "form_error": message,
            "submitted": submitted,
            "calculated_output_version": calculated_output_version,
        },
        status_code=status_code,
    )


def _format_datetime(value: object, display_timezone: ZoneInfo) -> str:
    if hasattr(value, "astimezone"):
        return str(
            value.astimezone(display_timezone).strftime(
                "%b %-d, %Y · %-I:%M\N{NO-BREAK SPACE}%p %Z"
            )
        )
    return str(value)


def _format_file_size(value: object) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(value)


def _format_duration(value: object) -> str:
    if not isinstance(value, timedelta):
        return str(value)
    seconds = max(0, round(value.total_seconds()))
    if seconds < 60:
        return f"{seconds} sec"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, remaining_minutes = divmod(minutes, 60)
    if remaining_minutes == 0:
        return f"{hours} hr"
    return f"{hours} hr {remaining_minutes} min"


def _format_source_name(value: object) -> str:
    source = str(value)
    if source in _SOURCE_DISPLAY_NAMES:
        return _SOURCE_DISPLAY_NAMES[source]
    return source.replace("_", " ").replace("-", " ").title()


def _format_dataset_name(value: object) -> str:
    words = str(value).replace("_", " ").replace("-", " ").split()
    return " ".join(_DATASET_ACRONYMS.get(word.lower(), word.title()) for word in words)


def _break_filename(value: object) -> Markup:
    safe_name = escape(str(value))
    return Markup(str(safe_name).replace("_", "_<wbr>"))


def _safe_http_url(value: object) -> str | None:
    url = str(value)
    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _format_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def main() -> None:
    defaults = WebSettings.from_environment()
    parser = argparse.ArgumentParser(description="Run the IFX Data Registry web application")
    parser.add_argument("--host", default=defaults.host)
    parser.add_argument("--port", type=int, default=defaults.port)
    parser.add_argument(
        "--root-path",
        default=defaults.root_path,
        help="ASGI root path when served beneath a reverse-proxy prefix (for example /registry)",
    )
    parser.add_argument("--bucket", default=defaults.bucket)
    parser.add_argument("--aws-region", default=defaults.aws_region)
    parser.add_argument("--s3-prefix", default=defaults.s3_prefix)
    parser.add_argument("--aws-credentials", type=Path, default=defaults.aws_credentials)
    parser.add_argument("--cure-credentials", type=Path, default=defaults.cure_credentials)
    parser.add_argument("--display-timezone", default=defaults.display_timezone)
    parser.add_argument("--state-dir", type=Path, default=defaults.state_directory)
    parser.add_argument(
        "--sources",
        type=Path,
        default=defaults.source_configuration,
        help="YAML file selecting the installed source adapters",
    )
    arguments = parser.parse_args()
    settings = WebSettings(
        state_directory=arguments.state_dir,
        source_configuration=arguments.sources,
        host=arguments.host,
        port=arguments.port,
        root_path=arguments.root_path,
        bucket=arguments.bucket,
        aws_region=arguments.aws_region,
        s3_prefix=arguments.s3_prefix,
        s3_endpoint_url=defaults.s3_endpoint_url,
        aws_role_arn=defaults.aws_role_arn,
        aws_external_id=defaults.aws_external_id,
        aws_credentials=arguments.aws_credentials,
        cure_credentials=arguments.cure_credentials,
        display_timezone=arguments.display_timezone,
        version_check_ttl_seconds=defaults.version_check_ttl_seconds,
    )
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        root_path=settings.root_path,
    )


if __name__ == "__main__":
    main()
