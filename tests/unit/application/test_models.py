"""Tests for caller-facing application result objects."""

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from ifx_registry.application.models import (
    DatasetDescription,
    ExternalDatasetDescription,
    MaterializedDataset,
)
from ifx_registry.domain.catalog import (
    PublishedExternalDatasetVersion,
    PublishedFile,
    PublishedSnapshot,
)
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    ExternalDatasetVersion,
    SourceVersion,
)


def _snapshot() -> PublishedSnapshot:
    content = b"id\n1\n"
    return PublishedSnapshot(
        dataset=DatasetId("example", "records"),
        version=SourceVersion("1", version_date=date(2026, 9, 8)),
        files=(
            PublishedFile(
                PurePosixPath("records.tsv"),
                "s3://registry/sources/example/records/1/records.tsv",
                len(content),
                hashlib.sha256(content).hexdigest(),
            ),
        ),
        downloaded_at=datetime(2026, 9, 8, 12, 30, tzinfo=UTC),
        published_at=datetime(2026, 9, 9, 8, 15, tzinfo=UTC),
        manifest_uri="s3://registry/sources/example/records/1/manifest.yaml",
        metadata={
            "nested": {
                "observed_on": date(2026, 9, 8),
                "observed_at": datetime(2026, 9, 8, 12, 30, tzinfo=UTC),
            }
        },
    )


def test_manifest_and_metadata_are_json_serializable(tmp_path: Path) -> None:
    dataset = MaterializedDataset(_snapshot(), tmp_path)

    manifest = dataset.manifest
    metadata = dataset.to_metadata()

    json.dumps(manifest)
    json.dumps(metadata)
    assert manifest["version_date"] == "2026-09-08"
    assert manifest["downloaded_at"] == "2026-09-08T12:30:00+00:00"
    assert manifest["extra"]["nested"]["observed_on"] == "2026-09-08"
    assert metadata["download_date"] == "2026-09-08"


def test_description_exposes_manifest_metadata_without_local_directory() -> None:
    description = DatasetDescription(_snapshot())

    assert description.snapshot_id == "example:records:1"
    assert description.file_names == ("records.tsv",)
    assert description.files[0].sha256 == hashlib.sha256(b"id\n1\n").hexdigest()
    assert description.total_size_bytes == 5
    assert description.version_date == date(2026, 9, 8)
    assert description.registered_at == datetime(2026, 9, 9, 8, 15, tzinfo=UTC)
    assert description.manifest["manifest_uri"].endswith("manifest.yaml")


def test_external_description_uses_the_persisted_sanitized_wire_shape() -> None:
    description = ExternalDatasetDescription(
        PublishedExternalDatasetVersion(
            value=ExternalDatasetVersion(
                dataset=DatasetId("chembl", "activity_database"),
                version=DatasetVersion("chembl36"),
                interface="mysql",
                access_mode="query",
                service_name="ChEMBL",
                observed_at=datetime(2026, 9, 10, tzinfo=UTC),
                metadata={"owner": "chembl"},
            ),
            published_at=datetime(2026, 9, 10, tzinfo=UTC),
            manifest_uri="s3://registry/external/chembl/activity_database/chembl36/manifest.yaml",
        )
    )

    assert description.kind == "external_dataset_version"
    assert description.manifest["kind"] == "external_source_registration"
    assert description.manifest["metadata"] == {"owner": "chembl"}
    assert "extra" not in description.manifest
