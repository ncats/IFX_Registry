"""Shared immutable-file mechanics for S3-backed dataset snapshots."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ifx_registry.domain.catalog import PublishedFile
from ifx_registry.domain.errors import (
    CatalogConsistencyError,
    InvalidPublicationError,
    SnapshotAlreadyExistsError,
)
from ifx_registry.domain.models import DatasetId
from ifx_registry.infrastructure.object_store import ObjectMetadata, ObjectStore

MANIFEST_FILE_NAME = "manifest.yaml"


@dataclass(frozen=True, slots=True)
class PublicationFile:
    local_path: Path
    relative_path: PurePosixPath
    content_type: str | None
    source_url: str | None


class S3ImmutableManifestStore:
    """List, read, and conditionally commit manifests under one S3 catalog root."""

    def __init__(self, objects: ObjectStore, root: str, *, prefix: str = ""):
        self._objects = objects
        self._root = root.strip("/")
        normalized_prefix = prefix.strip("/")
        self._prefix = f"{normalized_prefix}/" if normalized_prefix else ""

    @property
    def bucket(self) -> str:
        return self._objects.bucket

    def list_keys(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in self._objects.list_keys(f"{self._prefix}{self._root}/")
            if key.endswith(f"/{MANIFEST_FILE_NAME}")
        )

    def read(self, key: str) -> bytes | None:
        return self._objects.read_bytes(key)

    def key(self, dataset: DatasetId, version: str) -> str:
        return (
            f"{self._prefix}{self._root}/{dataset.source}/{dataset.dataset}/"
            f"{version}/{MANIFEST_FILE_NAME}"
        )

    def uri(self, key: str) -> str:
        return f"s3://{self._objects.bucket}/{key}"

    def commit(self, key: str, payload: bytes) -> bool:
        return self._objects.put_bytes_if_absent(
            key,
            payload,
            content_type="application/yaml",
        )


class S3DatasetFileStore:
    """Store files before an immutable manifest commit for one snapshot kind."""

    def __init__(
        self,
        objects: ObjectStore,
        root: str,
        *,
        prefix: str = "",
    ):
        self._objects = objects
        self._root = root.strip("/")
        self._manifests = S3ImmutableManifestStore(objects, root, prefix=prefix)
        normalized_prefix = prefix.strip("/")
        self._prefix = f"{normalized_prefix}/" if normalized_prefix else ""

    @property
    def bucket(self) -> str:
        return self._manifests.bucket

    def list_manifest_keys(self) -> tuple[str, ...]:
        return self._manifests.list_keys()

    def read_manifest(self, key: str) -> bytes | None:
        return self._manifests.read(key)

    def manifest_key(self, dataset: DatasetId, version: str) -> str:
        return self._manifests.key(dataset, version)

    def file_key(
        self,
        dataset: DatasetId,
        version: str,
        path: PurePosixPath,
    ) -> str:
        return f"{self._prefix}{self._root}/{dataset.source}/{dataset.dataset}/{version}/{path}"

    def uri(self, key: str) -> str:
        return self._manifests.uri(key)

    def inspect_files(
        self,
        dataset: DatasetId,
        version: str,
        files: tuple[PublicationFile, ...],
    ) -> tuple[tuple[PublicationFile, PublishedFile], ...]:
        result = []
        for file in files:
            if not file.local_path.is_file():
                raise InvalidPublicationError(
                    f"Snapshot file does not exist: {file.local_path}"
                )
            digest = sha256_file(file.local_path)
            key = self.file_key(dataset, version, file.relative_path)
            result.append(
                (
                    file,
                    PublishedFile(
                        relative_path=file.relative_path,
                        storage_uri=self.uri(key),
                        source_url=file.source_url,
                        size_bytes=file.local_path.stat().st_size,
                        sha256=digest,
                        content_type=file.content_type,
                    ),
                )
            )
        return tuple(result)

    def ensure_files(
        self,
        candidates: tuple[tuple[PublicationFile, PublishedFile], ...],
    ) -> None:
        for local_file, published_file in candidates:
            key = self.key_from_uri(published_file.storage_uri)
            existing = self._objects.stat(key)
            if existing is None:
                self._objects.put_file_if_absent(
                    local_file.local_path,
                    key,
                    content_type=local_file.content_type,
                    metadata={"sha256": published_file.sha256},
                )
                existing = self._objects.stat(key)
            self.verify_object(existing, published_file, key)
            if (
                local_file.local_path.stat().st_size != published_file.size_bytes
                or sha256_file(local_file.local_path) != published_file.sha256
            ):
                raise InvalidPublicationError(
                    f"Snapshot file changed while it was being registered: "
                    f"{local_file.local_path}"
                )

    def verify_files(self, files: tuple[PublishedFile, ...]) -> None:
        for file in files:
            key = self.key_from_uri(file.storage_uri)
            self.verify_object(self._objects.stat(key), file, key)

    @staticmethod
    def verify_object(
        actual: ObjectMetadata | None,
        expected: PublishedFile,
        key: str,
    ) -> None:
        if actual is None:
            raise CatalogConsistencyError(f"Uploaded Registry object is missing: {key}")
        if (
            actual.size_bytes != expected.size_bytes
            or actual.metadata.get("sha256") != expected.sha256
        ):
            raise SnapshotAlreadyExistsError(
                f"Registry object already exists with different content: {key}"
            )

    def commit_manifest(self, key: str, payload: bytes) -> bool:
        return self._manifests.commit(key, payload)

    def download(self, file: PublishedFile, destination: Path) -> None:
        self._objects.download_file(self.key_from_uri(file.storage_uri), destination)

    def key_from_uri(self, uri: str) -> str:
        expected_prefix = f"s3://{self._objects.bucket}/"
        if not uri.startswith(expected_prefix):
            raise CatalogConsistencyError(
                f"Published file is outside the Registry bucket: {uri}"
            )
        return uri.removeprefix(expected_prefix)

    def parse_file(self, manifest_key: str, value: object) -> PublishedFile:
        if not isinstance(value, dict):
            raise ValueError("manifest file entry must be a mapping")
        relative_path = PurePosixPath(str(value["path"]))
        object_key = f"{manifest_key.removesuffix(MANIFEST_FILE_NAME)}{relative_path}"
        return PublishedFile(
            relative_path=relative_path,
            storage_uri=self.uri(object_key),
            size_bytes=int(value["size_bytes"]),
            sha256=str(value["sha256"]),
            content_type=_optional_string(value.get("content_type")),
            source_url=_optional_string(value.get("source_url")),
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None
