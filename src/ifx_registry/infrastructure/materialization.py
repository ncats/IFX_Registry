"""Checksum-safe filesystem materialization for Registry consumers."""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import IO, overload
from uuid import uuid4

from ifx_registry.application.models import DerivedMaterializedDataset, MaterializedDataset
from ifx_registry.application.ports.snapshots import (
    PublishedFileReader,
    SnapshotMaterializationCache,
)
from ifx_registry.domain.catalog import (
    PublishedDatasetSnapshot,
    PublishedDerivedSnapshot,
    PublishedFile,
    PublishedSnapshot,
)
from ifx_registry.domain.errors import MaterializationError


class FileSystemSnapshotMaterializer(SnapshotMaterializationCache):
    """Materialize immutable files with process-safe locking and atomic writes."""

    def __init__(self, files: PublishedFileReader):
        self._files = files

    @overload
    def materialize(
        self,
        snapshot: PublishedSnapshot,
        destination: Path,
    ) -> MaterializedDataset: ...

    @overload
    def materialize(
        self,
        snapshot: PublishedDerivedSnapshot,
        destination: Path,
    ) -> DerivedMaterializedDataset: ...

    def materialize(
        self,
        snapshot: PublishedDatasetSnapshot,
        destination: Path,
    ) -> MaterializedDataset | DerivedMaterializedDataset:
        cache_root = Path(destination)
        cache_root.mkdir(parents=True, exist_ok=True)
        local_dir = self._local_dir(cache_root, snapshot)
        with self._snapshot_lock(cache_root, snapshot):
            local_dir.mkdir(parents=True, exist_ok=True)
            for published_file in snapshot.files:
                self._materialize_file(published_file, local_dir)
        if isinstance(snapshot, PublishedDerivedSnapshot):
            return DerivedMaterializedDataset(snapshot=snapshot, local_dir=local_dir)
        return MaterializedDataset(snapshot=snapshot, local_dir=local_dir)

    def materialize_file(
        self,
        snapshot: PublishedDatasetSnapshot,
        file: PublishedFile,
        destination: Path,
    ) -> Path:
        cache_root = Path(destination)
        cache_root.mkdir(parents=True, exist_ok=True)
        local_dir = self._local_dir(cache_root, snapshot)
        with self._snapshot_lock(cache_root, snapshot):
            local_dir.mkdir(parents=True, exist_ok=True)
            self._materialize_file(file, local_dir)
        return local_dir / file.relative_path

    @staticmethod
    def _local_dir(cache_root: Path, snapshot: PublishedDatasetSnapshot) -> Path:
        if isinstance(snapshot, PublishedDerivedSnapshot):
            return (
                cache_root
                / "derived"
                / snapshot.dataset.source
                / snapshot.dataset.dataset
                / snapshot.version.value
            )
        return (
            cache_root / snapshot.dataset.source / snapshot.dataset.dataset / snapshot.version.value
        )

    @staticmethod
    def _snapshot_lock(
        cache_root: Path,
        snapshot: PublishedDatasetSnapshot,
    ) -> AbstractContextManager[None]:
        lock_name = hashlib.sha256(snapshot.manifest_uri.encode("utf-8")).hexdigest()
        return _exclusive_lock(cache_root / ".locks" / f"{lock_name}.lock")

    def _materialize_file(self, file: PublishedFile, local_dir: Path) -> None:
        destination = local_dir / file.relative_path
        if _matches(destination, file):
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        try:
            self._files.download(file, temporary)
            if not _matches(temporary, file):
                raise MaterializationError(
                    f"Downloaded Registry file failed size or SHA-256 verification: "
                    f"{file.storage_uri}"
                )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _matches(path: Path, expected: PublishedFile) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == expected.size_bytes
        and _sha256(path) == expected.sha256
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        _lock(handle)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock(handle: IO[bytes]) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
