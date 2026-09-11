"""Dependency-freshness projections for the web catalog."""

import hashlib
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, cast

from ifx_registry.application.use_cases.browse_catalog import (
    CatalogKind,
    DependencyFreshness,
    GetRegistryDataset,
)
from ifx_registry.domain.catalog import (
    PublishedDerivedSnapshot,
    PublishedFile,
    PublishedSnapshot,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.errors import RegistryUnavailableError
from ifx_registry.domain.models import DatasetId, DatasetVersion, SnapshotRef, SourceVersion


class Catalog:
    def __init__(self, values: tuple[object, ...]):
        self.values = values

    def list_all(self) -> tuple[object, ...]:
        return self.values


class UnavailableCatalog(Catalog):
    def list_all(self) -> tuple[object, ...]:
        raise RegistryUnavailableError("catalog unavailable")


def _file() -> PublishedFile:
    return PublishedFile(
        PurePosixPath("records.tsv"),
        "s3://registry/example/records.tsv",
        5,
        hashlib.sha256(b"id\n1\n").hexdigest(),
    )


def _source(version: str) -> PublishedSnapshot:
    return PublishedSnapshot(
        dataset=DatasetId("source", "records"),
        version=SourceVersion(version),
        files=(_file(),),
        downloaded_at=datetime(2026, 9, 1, tzinfo=UTC),
        published_at=datetime(2026, 9, int(version), tzinfo=UTC),
        manifest_uri=f"s3://registry/sources/source/records/{version}/manifest.yaml",
    )


def _derived() -> PublishedDerivedSnapshot:
    reference = SnapshotRef.source("source:records:1")
    return PublishedDerivedSnapshot(
        dataset=DatasetId("derived", "records"),
        version=DatasetVersion("1"),
        files=(_file(),),
        inputs=(
            RegisteredSnapshotRef(
                reference,
                "s3://registry/sources/source/records/1/manifest.yaml",
            ),
        ),
        producer=None,
        transform={},
        validation={},
        published_at=datetime(2026, 9, 3, tzinfo=UTC),
        manifest_uri="s3://registry/derived/derived/records/1/manifest.yaml",
        build_key=None,
        publication_fingerprint="a" * 64,
    )


def _overview(source_catalog: Catalog) -> Any:
    return GetRegistryDataset(
        cast(Any, source_catalog),
        cast(Any, Catalog((_derived(),))),
        cast(Any, Catalog(())),
    ).execute(CatalogKind.DERIVED, DatasetId("derived", "records"))


def test_missing_pin_is_distinct_from_a_known_newer_version() -> None:
    dependency = _overview(Catalog((_source("2"),))).latest_lineage.dependencies[0]

    assert dependency.freshness is DependencyFreshness.MISSING
    assert dependency.latest_version is not None
    assert dependency.latest_version.value == "2"


def test_same_source_version_id_is_latest_despite_discovery_timestamp() -> None:
    dependency = _overview(Catalog((_source("1"),))).latest_lineage.dependencies[0]

    assert dependency.freshness is DependencyFreshness.LATEST_REGISTERED


def test_dependency_comparison_failure_does_not_hide_derived_dataset() -> None:
    dependency = _overview(UnavailableCatalog(())).latest_lineage.dependencies[0]

    assert dependency.freshness is DependencyFreshness.UNAVAILABLE
    assert dependency.latest_version is None
