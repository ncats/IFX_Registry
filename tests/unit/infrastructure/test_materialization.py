import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any, cast

import pytest

from ifx_registry.application.ports.snapshots import PublishedFileReader
from ifx_registry.application.use_cases.materialize_dataset import MaterializeDataset
from ifx_registry.application.use_cases.materialize_file import MaterializeFile
from ifx_registry.domain.catalog import PublishedDerivedSnapshot, PublishedFile, PublishedSnapshot
from ifx_registry.domain.errors import MaterializationError, MultipleDatasetFilesError
from ifx_registry.domain.models import DatasetId, DatasetVersion, SourceVersion
from ifx_registry.infrastructure.materialization import FileSystemSnapshotMaterializer
from ifx_registry.infrastructure.s3_snapshots import S3PublishedSnapshotRepository

from ...fakes import FakeObjectStore


def _published(content: bytes = b"id\n1\n") -> PublishedSnapshot:
    return PublishedSnapshot(
        dataset=DatasetId("example", "records"),
        version=SourceVersion("1"),
        files=(
            PublishedFile(
                PurePosixPath("nested/records.tsv"),
                "s3://test-registry/sources/example/records/1/nested/records.tsv",
                len(content),
                hashlib.sha256(content).hexdigest(),
            ),
        ),
        downloaded_at=datetime(2026, 9, 9, tzinfo=UTC),
        published_at=datetime(2026, 9, 9, tzinfo=UTC),
        manifest_uri="s3://test-registry/sources/example/records/1/manifest.yaml",
    )


class RecordingReader(PublishedFileReader):
    def __init__(self, content: bytes):
        self.content = content
        self.calls = 0
        self._lock = Lock()

    def download(self, file: PublishedFile, destination: Path) -> None:
        del file
        with self._lock:
            self.calls += 1
        destination.write_bytes(self.content)


def test_materialization_verifies_and_reuses_checksum_matching_cache(tmp_path: Path) -> None:
    reader = RecordingReader(b"id\n1\n")
    materializer = FileSystemSnapshotMaterializer(reader)
    snapshot = _published()

    first = materializer.materialize(snapshot, tmp_path)
    second = materializer.materialize(snapshot, tmp_path)

    assert first.file("nested/records.tsv").read_bytes() == b"id\n1\n"
    assert second.file("nested/records.tsv") == first.file("nested/records.tsv")
    assert reader.calls == 1


def test_materialization_replaces_corrupt_cache_and_removes_partial_file(
    tmp_path: Path,
) -> None:
    reader = RecordingReader(b"id\n1\n")
    materializer = FileSystemSnapshotMaterializer(reader)
    snapshot = _published()
    result = materializer.materialize(snapshot, tmp_path)
    result.file().write_bytes(b"corrupt")

    repaired = materializer.materialize(snapshot, tmp_path)

    assert repaired.file().read_bytes() == b"id\n1\n"
    assert reader.calls == 2
    assert not tuple(repaired.local_dir.rglob("*.part"))


def test_materialization_rejects_bad_download_and_cleans_partial(tmp_path: Path) -> None:
    materializer = FileSystemSnapshotMaterializer(RecordingReader(b"wrong"))

    with pytest.raises(MaterializationError, match="SHA-256"):
        materializer.materialize(_published(), tmp_path)

    assert not tuple(tmp_path.rglob("*.part"))


def test_concurrent_materializers_download_one_copy(tmp_path: Path) -> None:
    reader = RecordingReader(b"id\n1\n")
    materializer = FileSystemSnapshotMaterializer(reader)
    snapshot = _published()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: materializer.materialize(snapshot, tmp_path), range(2)))

    assert reader.calls == 1
    assert results[0].file().read_bytes() == b"id\n1\n"
    assert results[1].file() == results[0].file()


def test_materialize_use_case_requires_exact_registered_version(tmp_path: Path) -> None:
    objects = FakeObjectStore()
    repository = S3PublishedSnapshotRepository(objects)
    use_case = MaterializeDataset(repository, FileSystemSnapshotMaterializer(repository))

    with pytest.raises(LookupError, match="Published snapshot not found"):
        use_case.execute(
            DatasetId("example", "records"),
            "missing",
            destination=tmp_path,
        )


def test_materialize_file_downloads_only_the_selected_file(tmp_path: Path) -> None:
    content = b"id\n1\n"
    first = _published(content)
    second_file = PublishedFile(
        PurePosixPath("other.tsv"),
        "s3://test-registry/sources/example/records/1/other.tsv",
        len(content),
        hashlib.sha256(content).hexdigest(),
    )
    snapshot = PublishedSnapshot(
        dataset=first.dataset,
        version=first.version,
        files=first.files + (second_file,),
        downloaded_at=first.downloaded_at,
        published_at=first.published_at,
        manifest_uri=first.manifest_uri,
    )

    class Catalog:
        def list_all(self) -> tuple[PublishedSnapshot, ...]:
            return (snapshot,)

        def get(self, dataset: DatasetId, version: str) -> PublishedSnapshot:
            assert dataset == snapshot.dataset
            assert version == snapshot.version.value
            return snapshot

    reader = RecordingReader(content)
    use_case = MaterializeFile(
        cast(Any, Catalog()),
        FileSystemSnapshotMaterializer(reader),
    )

    selected = use_case.execute(
        snapshot.dataset,
        snapshot.version.value,
        "other.tsv",
        destination=tmp_path,
    )

    assert selected.read_bytes() == content
    assert not (tmp_path / "example" / "records" / "1" / "nested" / "records.tsv").exists()
    assert reader.calls == 1

    with pytest.raises(MultipleDatasetFilesError, match="nested/records.tsv, other.tsv"):
        use_case.execute(
            snapshot.dataset,
            snapshot.version.value,
            None,
            destination=tmp_path,
        )


def test_derived_cache_is_namespaced_away_from_source_cache(tmp_path: Path) -> None:
    source = _published()
    derived = PublishedDerivedSnapshot(
        dataset=source.dataset,
        version=DatasetVersion(source.version.value),
        files=source.files,
        inputs=(),
        producer=None,
        transform={},
        validation={},
        published_at=source.published_at,
        manifest_uri="s3://test-registry/derived/example/records/1/manifest.yaml",
        build_key="a" * 64,
        publication_fingerprint="a" * 64,
    )
    materializer = FileSystemSnapshotMaterializer(RecordingReader(b"id\n1\n"))

    source_result = materializer.materialize(source, tmp_path)
    derived_result = materializer.materialize(derived, tmp_path)

    assert source_result.local_dir == tmp_path / "example" / "records" / "1"
    assert derived_result.local_dir == tmp_path / "derived" / "example" / "records" / "1"
