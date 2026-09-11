"""Contract tests for S3 manifest publication and catalog reads."""

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

import pytest
import yaml

from ifx_registry.domain.errors import (
    CatalogConsistencyError,
    InvalidPublicationError,
    SnapshotAlreadyExistsError,
)
from ifx_registry.domain.models import DatasetId, SnapshotFile, SourceSnapshot, SourceVersion
from ifx_registry.infrastructure.object_store import ObjectMetadata
from ifx_registry.infrastructure.s3_snapshots import S3PublishedSnapshotRepository

from ...fakes import FakeObjectStore


def _snapshot(root: Path, content: str = "id\n1\n") -> SourceSnapshot:
    path = root / "records.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return SourceSnapshot(
        dataset=DatasetId("example", "records"),
        version=SourceVersion(
            "1",
            version_date=date(2026, 9, 8),
            discovered_at=datetime(2026, 9, 8, 12, tzinfo=UTC),
            evidence={"release": "1"},
        ),
        files=(
            SnapshotFile(
                local_path=path,
                relative_path=PurePosixPath("records.tsv"),
                source_url="https://example.org/records.tsv",
                content_type="text/tab-separated-values",
            ),
        ),
        downloaded_at=datetime(2026, 9, 8, 13, tzinfo=UTC),
        homepage="https://example.org/",
        upstream_urls=("https://example.org/records.tsv",),
    )


def test_publish_uploads_files_before_committing_manifest(tmp_path: Path) -> None:
    objects = FakeObjectStore()
    repository = S3PublishedSnapshotRepository(objects)

    published = repository.publish(_snapshot(tmp_path))

    assert objects.actions == [
        ("upload", "sources/example/records/1/records.tsv"),
        ("commit", "sources/example/records/1/manifest.yaml"),
    ]
    assert published.snapshot_id == "example:records:1"
    assert published.files[0].sha256 == hashlib.sha256(b"id\n1\n").hexdigest()
    manifest = yaml.safe_load(objects.objects[objects.actions[-1][1]])
    assert manifest["kind"] == "source_snapshot"
    assert manifest["files"][0]["storage_uri"] == (
        "s3://test-registry/sources/example/records/1/records.tsv"
    )


def test_publishing_identical_snapshot_is_idempotent(tmp_path: Path) -> None:
    objects = FakeObjectStore()
    repository = S3PublishedSnapshotRepository(objects)
    first = repository.publish(_snapshot(tmp_path / "one"))

    second = repository.publish(_snapshot(tmp_path / "two"))

    assert second == first
    assert len(objects.actions) == 2


def test_publishing_different_content_cannot_replace_manifest(tmp_path: Path) -> None:
    repository = S3PublishedSnapshotRepository(FakeObjectStore())
    repository.publish(_snapshot(tmp_path / "one"))

    with pytest.raises(SnapshotAlreadyExistsError, match="immutable"):
        repository.publish(_snapshot(tmp_path / "two", "id\n2\n"))


def test_publishing_changed_provenance_cannot_claim_idempotence(tmp_path: Path) -> None:
    objects = FakeObjectStore()
    repository = S3PublishedSnapshotRepository(objects)
    repository.publish(_snapshot(tmp_path / "one"))
    changed = _snapshot(tmp_path / "two")
    changed = SourceSnapshot(
        dataset=changed.dataset,
        version=changed.version,
        files=changed.files,
        downloaded_at=changed.downloaded_at,
        homepage="https://different.example.org/",
    )

    with pytest.raises(SnapshotAlreadyExistsError, match="different"):
        repository.publish(changed)


def test_json_metadata_distinguishes_numbers_from_booleans(tmp_path: Path) -> None:
    repository = S3PublishedSnapshotRepository(FakeObjectStore())
    original = _snapshot(tmp_path / "one")
    original = SourceSnapshot(
        dataset=original.dataset,
        version=original.version,
        files=original.files,
        downloaded_at=original.downloaded_at,
        metadata={"validation": {"rows": 1}},
    )
    repository.publish(original)
    changed = _snapshot(tmp_path / "two")
    changed = SourceSnapshot(
        dataset=changed.dataset,
        version=changed.version,
        files=changed.files,
        downloaded_at=changed.downloaded_at,
        metadata={"validation": {"rows": True}},
    )

    with pytest.raises(SnapshotAlreadyExistsError, match="different"):
        repository.publish(changed)


def test_concurrent_file_writer_cannot_overwrite_or_commit(tmp_path: Path) -> None:
    class RacingObjectStore(FakeObjectStore):
        def stat(self, key: str):  # type: ignore[no-untyped-def]
            if key.endswith("records.tsv") and key not in self.objects:
                return None
            return super().stat(key)

        def put_file_if_absent(self, path, key, *, content_type, metadata):  # type: ignore[no-untyped-def]
            if key.endswith("records.tsv"):
                competitor = tmp_path / "competitor.tsv"
                competitor.write_text("different", encoding="utf-8")
                content = competitor.read_bytes()
                self.objects[key] = content
                self.object_metadata[key] = ObjectMetadata(
                    key=key,
                    size_bytes=len(content),
                    content_type=content_type,
                    metadata={"sha256": "0" * 64},
                )
                return False
            return super().put_file_if_absent(
                path,
                key,
                content_type=content_type,
                metadata=metadata,
            )

    objects = RacingObjectStore()
    repository = S3PublishedSnapshotRepository(objects)

    with pytest.raises(SnapshotAlreadyExistsError, match="different content"):
        repository.publish(_snapshot(tmp_path / "publisher"))

    assert "sources/example/records/1/manifest.yaml" not in objects.objects


def test_concurrent_manifest_writer_accepts_identical_winner(tmp_path: Path) -> None:
    class RacingObjectStore(FakeObjectStore):
        def put_bytes_if_absent(self, key, payload, *, content_type):  # type: ignore[no-untyped-def]
            if key.endswith("manifest.yaml") and key not in self.objects:
                self.objects[key] = payload
                return False
            return super().put_bytes_if_absent(key, payload, content_type=content_type)

    repository = S3PublishedSnapshotRepository(RacingObjectStore())

    assert repository.publish(_snapshot(tmp_path)).snapshot_id == "example:records:1"


def test_mutating_caller_file_cannot_poison_an_uncommitted_object(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    caller_file = snapshot.files[0].local_path

    class MutatingObjectStore(FakeObjectStore):
        mutated = False

        def put_file_if_absent(
            self, path, key, *, content_type, metadata  # type: ignore[no-untyped-def]
        ):
            if not self.mutated:
                caller_file.write_text("id\n2\n", encoding="utf-8")
                self.mutated = True
            return super().put_file_if_absent(
                path, key, content_type=content_type, metadata=metadata
            )

    objects = MutatingObjectStore()
    repository = S3PublishedSnapshotRepository(objects)

    with pytest.raises(InvalidPublicationError, match="changed"):
        repository.publish(snapshot)

    object_key = "sources/example/records/1/records.tsv"
    assert object_key not in objects.objects
    assert "sources/example/records/1/manifest.yaml" not in objects.objects

    caller_file.write_text("id\n1\n", encoding="utf-8")
    assert repository.publish(snapshot).snapshot_id == "example:records:1"


def test_catalog_uses_actual_bucket_for_legacy_storage_uris(tmp_path: Path) -> None:
    objects = FakeObjectStore("current-bucket")
    repository = S3PublishedSnapshotRepository(objects)
    repository.publish(_snapshot(tmp_path))
    key = "sources/example/records/1/manifest.yaml"
    manifest = yaml.safe_load(objects.objects[key])
    manifest["files"][0]["storage_uri"] = "s3://old-bucket/wrong/key"
    objects.objects[key] = yaml.safe_dump(manifest).encode()

    published = repository.list_all()[0]

    assert published.files[0].storage_uri == (
        "s3://current-bucket/sources/example/records/1/records.tsv"
    )


def test_catalog_reads_representative_legacy_manifest() -> None:
    objects = FakeObjectStore("current-bucket")
    key = "sources/example/records/1/manifest.yaml"
    objects.objects[key] = b"""\
kind: source_snapshot
schema_version: 1
source: example
dataset: records
snapshot_id: example:records:1
version: '1'
version_date: 2026-09-08
download_date: 2026-09-08
created_at: 2026-09-08T14:00:00+00:00
upstream:
  homepage: https://example.org/
  urls:
    - https://example.org/records.tsv
files:
  - path: records.tsv
    size_bytes: 5
    sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    source_url: https://example.org/records.tsv
    storage_uri: s3://legacy-bucket/sources/example/records/1/records.tsv
extra:
  version_method:
    type: release_page
    evidence:
      release: '1'
"""

    published = S3PublishedSnapshotRepository(objects).list_all()[0]

    assert published.snapshot_id == "example:records:1"
    assert published.version.evidence == {"release": "1"}
    assert published.files[0].storage_uri.startswith("s3://current-bucket/")


def test_catalog_rejects_manifest_identity_that_disagrees_with_key() -> None:
    objects = FakeObjectStore()
    key = "sources/example/records/1/manifest.yaml"
    objects.objects[key] = yaml.safe_dump(
        {
            "kind": "source_snapshot",
            "schema_version": 1,
            "source": "another",
            "dataset": "records",
            "version": "1",
        }
    ).encode()
    repository = S3PublishedSnapshotRepository(objects)

    with pytest.raises(CatalogConsistencyError, match="does not match"):
        repository.list_all()
