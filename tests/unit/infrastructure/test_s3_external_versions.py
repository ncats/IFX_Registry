"""S3 contract tests for metadata-only external dataset versions."""

from datetime import UTC, date, datetime

import pytest

from ifx_registry import ExternalDatasetVersion
from ifx_registry.domain.errors import InvalidPublicationError, SnapshotAlreadyExistsError
from ifx_registry.domain.models import DatasetId, DatasetVersion
from ifx_registry.infrastructure.s3_external_versions import (
    S3ExternalDatasetVersionRepository,
)

from ...fakes import FakeObjectStore


def _value(service_name: str = "ChEMBL activity database") -> ExternalDatasetVersion:
    return ExternalDatasetVersion(
        dataset=DatasetId("chembl", "activity_database"),
        version=DatasetVersion("chembl36", version_date=date(2026, 8, 28)),
        interface="mysql",
        access_mode="query",
        service_name=service_name,
        observed_at=datetime(2026, 9, 10, tzinfo=UTC),
        documentation_url="https://www.ebi.ac.uk/chembl/",
        version_check={"type": "schema_release"},
        version_evidence={"release": "chembl_36"},
    )


def test_publish_is_manifest_only_and_idempotent() -> None:
    objects = FakeObjectStore()
    repository = S3ExternalDatasetVersionRepository(objects)

    first = repository.publish(_value())
    retried = repository.publish(_value())

    assert retried == first
    assert objects.actions == [
        ("commit", "external/chembl/activity_database/chembl36/manifest.yaml")
    ]
    assert first.snapshot_id == "chembl:activity_database:chembl36"
    assert first.value.interface == "mysql"


def test_changed_external_assertion_cannot_replace_registered_version() -> None:
    repository = S3ExternalDatasetVersionRepository(FakeObjectStore())
    repository.publish(_value())

    with pytest.raises(SnapshotAlreadyExistsError, match="immutable"):
        repository.publish(_value("Different service"))


def test_concurrent_external_registration_accepts_identical_winner() -> None:
    class RacingObjectStore(FakeObjectStore):
        def put_bytes_if_absent(self, key, payload, *, content_type):  # type: ignore[no-untyped-def]
            if key not in self.objects:
                self.objects[key] = payload
                return False
            return super().put_bytes_if_absent(key, payload, content_type=content_type)

    repository = S3ExternalDatasetVersionRepository(RacingObjectStore())

    assert repository.publish(_value()).snapshot_id == "chembl:activity_database:chembl36"


def test_external_manifest_never_serializes_credentials() -> None:
    objects = FakeObjectStore()
    repository = S3ExternalDatasetVersionRepository(objects)

    repository.publish(_value())

    manifest = next(iter(objects.objects.values())).decode("utf-8")
    assert "password" not in manifest
    assert "credential" not in manifest
    assert "secret" not in manifest


def test_mutated_external_mapping_is_revalidated_before_commit() -> None:
    objects = FakeObjectStore()
    repository = S3ExternalDatasetVersionRepository(objects)
    value = _value()
    assert isinstance(value.metadata, dict)
    value.metadata["note"] = "postgresql://user:password@database.internal/chembl"

    with pytest.raises(InvalidPublicationError, match="connection URI"):
        repository.publish(value)

    assert objects.objects == {}


def test_legacy_manifest_is_read_without_exposing_connection_details() -> None:
    objects = FakeObjectStore("current-bucket")
    key = "external/chembl/activity_database/chembl36/manifest.yaml"
    objects.objects[key] = b"""\
kind: external_source_registration
schema_version: 1
source: chembl
dataset: activity_database
registration_id: chembl:activity_database:chembl36
version: chembl36
registered_date: '2026-06-10'
connection:
  type: mysql
  host: database.internal
  schema: chembl36
  credential_ref: secrets/database.yaml
access:
  mode: query
  interface: sql
extra:
  version_method:
    type: database_schema
    description: Read the provider schema release.
    evidence:
      release: chembl36
"""

    published = S3ExternalDatasetVersionRepository(objects).get(
        DatasetId("chembl", "activity_database"), "chembl36"
    )

    assert published.value.interface == "mysql"
    assert published.value.access_mode == "query"
    assert published.value.documentation_url is None
    assert published.value.version_evidence == {"release": "chembl36"}
    rendered = str(published)
    assert "credential_ref" not in rendered
    assert "database.internal" not in rendered
