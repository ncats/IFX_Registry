"""S3 contract tests for caller-produced derived datasets."""

from datetime import date
from pathlib import Path, PurePosixPath

import pytest
import yaml

from ifx_registry import ProducerIdentity, SnapshotRef
from ifx_registry.domain.catalog import RegisteredSnapshotRef
from ifx_registry.domain.errors import SnapshotAlreadyExistsError
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    DerivedSnapshot,
    DerivedSnapshotFile,
)
from ifx_registry.infrastructure.s3_derived_snapshots import S3DerivedSnapshotRepository

from ...fakes import FakeObjectStore


def _producer() -> ProducerIdentity:
    return ProducerIdentity(
        "ifx_harmonizers",
        "2.2.0",
        "https://github.com/ncats/IFX_harmonizers",
        "a" * 40,
    )


def _input() -> RegisteredSnapshotRef:
    return RegisteredSnapshotRef(
        SnapshotRef.source("uniprot:human:2026_03"),
        "s3://test-registry/sources/uniprot/human/2026_03/manifest.yaml",
        "b" * 64,
    )


def _snapshot(root: Path, content: str = "id\n1\n") -> DerivedSnapshot:
    path = root / "producer-output.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return DerivedSnapshot(
        dataset=DatasetId("ifx_harmonizers", "targets"),
        version=DatasetVersion("2.2.0", version_date=date(2026, 9, 10)),
        files=(DerivedSnapshotFile(path, PurePosixPath("nested/targets.tsv")),),
        inputs=(_input().ref,),
        producer=_producer(),
        transform={"name": "target_harmonization", "version": "2"},
        validation={"rows": 1, "unmapped": 0},
        metadata={"reviewed": True},
    )


def test_publish_commits_files_then_manifest_and_reads_description(tmp_path: Path) -> None:
    objects = FakeObjectStore()
    repository = S3DerivedSnapshotRepository(objects)

    published = repository.publish(_snapshot(tmp_path), (_input(),))

    assert objects.actions == [
        ("upload", "derived/ifx_harmonizers/targets/2.2.0/nested/targets.tsv"),
        ("commit", "derived/ifx_harmonizers/targets/2.2.0/manifest.yaml"),
    ]
    assert published.snapshot_id == "ifx_harmonizers:targets:2.2.0"
    assert published.inputs[0].snapshot_id == "uniprot:human:2026_03"
    assert published.producer == _producer()
    assert published.validation == {"rows": 1, "unmapped": 0}
    assert published.manifest_sha256 is not None


def test_identical_retry_is_idempotent_but_changed_validation_is_rejected(
    tmp_path: Path,
) -> None:
    objects = FakeObjectStore()
    repository = S3DerivedSnapshotRepository(objects)
    first = repository.publish(_snapshot(tmp_path / "one"), (_input(),))

    retried = repository.publish(_snapshot(tmp_path / "two"), (_input(),))

    assert retried == first
    assert len(objects.actions) == 2
    changed = _snapshot(tmp_path / "three")
    changed = DerivedSnapshot(
        dataset=changed.dataset,
        version=changed.version,
        files=changed.files,
        inputs=changed.inputs,
        producer=changed.producer,
        transform=changed.transform,
        validation={"rows": 2},
        metadata=changed.metadata,
    )
    with pytest.raises(SnapshotAlreadyExistsError, match="different provenance"):
        repository.publish(changed, (_input(),))


def test_multi_input_retry_is_idempotent_across_dependency_order(
    tmp_path: Path,
) -> None:
    objects = FakeObjectStore()
    repository = S3DerivedSnapshotRepository(objects)
    first_input = RegisteredSnapshotRef(
        SnapshotRef.source("source:first:1"),
        "s3://test-registry/sources/source/first/1/manifest.yaml",
        "c" * 64,
        "first",
    )
    second_input = RegisteredSnapshotRef(
        SnapshotRef.source("source:second:1"),
        "s3://test-registry/sources/source/second/1/manifest.yaml",
        "d" * 64,
        "second",
    )
    base = _snapshot(tmp_path / "one")
    snapshot = DerivedSnapshot(
        dataset=base.dataset,
        version=base.version,
        files=base.files,
        inputs=(second_input.ref, first_input.ref),
        producer=base.producer,
        transform=base.transform,
        validation=base.validation,
        metadata=base.metadata,
    )

    first = repository.publish(snapshot, (second_input, first_input))
    retried = repository.publish(snapshot, (first_input, second_input))

    assert retried == first
    assert [item.slot for item in retried.inputs] == ["first", "second"]


def test_json_distinct_validation_is_not_an_identical_retry(tmp_path: Path) -> None:
    objects = FakeObjectStore()
    repository = S3DerivedSnapshotRepository(objects)
    original = _snapshot(tmp_path / "one")
    original = DerivedSnapshot(
        dataset=original.dataset,
        version=original.version,
        files=original.files,
        inputs=original.inputs,
        producer=original.producer,
        transform=original.transform,
        validation={"rows": 1},
        metadata=original.metadata,
    )
    repository.publish(original, (_input(),))
    changed = _snapshot(tmp_path / "two")
    changed = DerivedSnapshot(
        dataset=changed.dataset,
        version=changed.version,
        files=changed.files,
        inputs=changed.inputs,
        producer=changed.producer,
        transform=changed.transform,
        validation={"rows": True},
        metadata=changed.metadata,
    )

    with pytest.raises(SnapshotAlreadyExistsError, match="different provenance"):
        repository.publish(changed, (_input(),))


def test_catalog_reads_representative_legacy_derived_manifest() -> None:
    objects = FakeObjectStore("current-bucket")
    key = "derived/example/records/deps-not-a-date/manifest.yaml"
    objects.objects[key] = b"""\
kind: derived_snapshot
schema_version: 1
source: example
dataset: records
snapshot_id: example:records:deps-not-a-date
version: deps-not-a-date
version_date: deps-not-a-date
created_at: 2026-09-02T12:00:00+00:00
derived_from:
  - source: producer
    dataset: records
    version: '1'
    snapshot_id: producer:records:1
    manifest_uri: s3://old-bucket/derived/producer/records/1/manifest.yaml
transform:
  name: example_transform
  version: 1
build_key: abc123
files:
  - path: records.tsv
    size_bytes: 5
    sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    storage_uri: s3://old-bucket/derived/example/records/1/records.tsv
stats:
  rows: 1
"""

    published = S3DerivedSnapshotRepository(objects).get(
        DatasetId("example", "records"), "deps-not-a-date"
    )

    assert published.producer is None
    assert published.version.version_date is None
    assert published.inputs[0].ref == SnapshotRef.derived("producer:records:1")
    assert published.validation == {"rows": 1}
    assert published.build_key == "abc123"
    assert len(published.publication_fingerprint) == 64
    assert published.files[0].storage_uri == (
        "s3://current-bucket/derived/example/records/deps-not-a-date/records.tsv"
    )
    assert yaml.safe_load(objects.objects[key])["kind"] == "derived_snapshot"
