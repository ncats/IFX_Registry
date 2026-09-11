"""Manifest-backed source snapshot catalog and publisher for S3."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from ifx_registry.application.ports.snapshots import (
    PublishedFileReader,
    PublishedSnapshotCatalog,
    SourceSnapshotPublisher,
)
from ifx_registry.domain.catalog import PublishedFile, PublishedSnapshot
from ifx_registry.domain.errors import (
    CatalogConsistencyError,
    SnapshotAlreadyExistsError,
    SnapshotNotFoundError,
    SourceContractError,
)
from ifx_registry.domain.models import DatasetId, SourceSnapshot, SourceVersion
from ifx_registry.infrastructure.object_store import ObjectStore
from ifx_registry.infrastructure.s3_dataset_files import (
    PublicationFile,
    S3DatasetFileStore,
)

MANIFEST_SCHEMA_VERSION = 1


class S3PublishedSnapshotRepository(
    PublishedSnapshotCatalog,
    SourceSnapshotPublisher,
    PublishedFileReader,
):
    """Read and atomically publish canonical source-snapshot manifests."""

    def __init__(self, objects: ObjectStore, *, prefix: str = ""):
        self._files = S3DatasetFileStore(objects, "sources", prefix=prefix)

    def list_all(self) -> tuple[PublishedSnapshot, ...]:
        manifest_keys = self._files.list_manifest_keys()
        snapshots = [self._load_manifest(key) for key in manifest_keys]
        return tuple(sorted(snapshots, key=lambda item: item.published_at, reverse=True))

    def get(self, dataset: DatasetId, version: str) -> PublishedSnapshot:
        normalized_version = SourceVersion(version).value
        key = self._manifest_key(dataset, normalized_version)
        payload = self._files.read_manifest(key)
        if payload is None:
            raise SnapshotNotFoundError(f"Published snapshot not found: {dataset}:{version}")
        return self._parse_manifest(key, payload)

    def download(self, file: PublishedFile, destination: Path) -> None:
        self._files.download(file, destination)

    def publish(self, snapshot: SourceSnapshot) -> PublishedSnapshot:
        manifest_key = self._manifest_key(snapshot.dataset, snapshot.version.value)
        existing_payload = self._files.read_manifest(manifest_key)
        candidates = self._inspect_local_files(snapshot)
        if existing_payload is not None:
            existing = self._parse_manifest(manifest_key, existing_payload)
            if _same_publication(existing, snapshot, candidates):
                self._verify_published_files(existing.files)
                return existing
            raise SnapshotAlreadyExistsError(
                "Published snapshot is immutable and has different metadata or files: "
                f"{snapshot.snapshot_id}"
            )

        self._files.ensure_files(candidates)

        published_at = datetime.now(UTC)
        payload = self._manifest_payload(snapshot, candidates, manifest_key, published_at)
        manifest_bytes = yaml.safe_dump(payload, sort_keys=False).encode("utf-8")
        if not self._files.commit_manifest(manifest_key, manifest_bytes):
            winner_payload = self._files.read_manifest(manifest_key)
            if winner_payload is None:
                raise CatalogConsistencyError(
                    f"Manifest commit was rejected but no manifest exists: {manifest_key}"
                )
            winner = self._parse_manifest(manifest_key, winner_payload)
            if _same_publication(winner, snapshot, candidates):
                self._verify_published_files(winner.files)
                return winner
            raise SnapshotAlreadyExistsError(
                f"Published snapshot was committed concurrently with different files: "
                f"{snapshot.snapshot_id}"
            )
        committed = self._load_manifest(manifest_key)
        self._verify_published_files(committed.files)
        return committed

    def _inspect_local_files(
        self, snapshot: SourceSnapshot
    ) -> tuple[tuple[PublicationFile, PublishedFile], ...]:
        for snapshot_file in snapshot.files:
            if not snapshot_file.local_path.is_file():
                raise SourceContractError(
                    f"Snapshot file does not exist: {snapshot_file.local_path}"
                )
        return self._files.inspect_files(
            snapshot.dataset,
            snapshot.version.value,
            tuple(
                PublicationFile(
                    file.local_path,
                    file.relative_path,
                    file.content_type,
                    file.source_url,
                )
                for file in snapshot.files
            ),
        )

    def _verify_published_files(self, files: tuple[PublishedFile, ...]) -> None:
        self._files.verify_files(files)

    def _load_manifest(self, key: str) -> PublishedSnapshot:
        payload = self._files.read_manifest(key)
        if payload is None:
            raise SnapshotNotFoundError(f"Published manifest not found: {key}")
        return self._parse_manifest(key, payload)

    def _parse_manifest(self, key: str, content: bytes) -> PublishedSnapshot:
        try:
            payload = yaml.safe_load(content)
            if not isinstance(payload, dict):
                raise ValueError("manifest root must be a mapping")
            if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
                raise ValueError("unsupported schema_version")
            if payload.get("kind", "source_snapshot") != "source_snapshot":
                raise ValueError("manifest kind is not source_snapshot")
            dataset = DatasetId(str(payload["source"]), str(payload["dataset"]))
            version_value = str(payload["version"])
            if key != self._manifest_key(dataset, version_value):
                raise ValueError("manifest identity does not match its S3 key")
            version_date = _optional_date(payload.get("version_date"))
            published_at = _datetime(payload.get("created_at") or payload.get("registered_at"))
            downloaded_at = _datetime(payload.get("downloaded_at") or payload.get("download_date"))
            extra = _mapping(payload.get("extra") or payload.get("metadata"))
            version_method = _mapping(extra.get("version_method"))
            evidence = _mapping(payload.get("version_evidence")) or _mapping(
                version_method.get("evidence")
            )
            files = tuple(self._parse_file(key, item) for item in _sequence(payload["files"]))
            upstream = _mapping(payload.get("upstream"))
            upstream_urls = upstream.get("urls") or payload.get("upstream_urls") or ()
            return PublishedSnapshot(
                dataset=dataset,
                version=SourceVersion(
                    version_value,
                    version_date=version_date,
                    discovered_at=_datetime(payload.get("discovered_at") or published_at),
                    evidence=evidence,
                ),
                files=files,
                downloaded_at=downloaded_at,
                published_at=published_at,
                manifest_uri=self._uri(key),
                homepage=_optional_string(upstream.get("homepage") or payload.get("homepage")),
                upstream_urls=tuple(str(value) for value in upstream_urls),
                metadata=extra,
                manifest_sha256=hashlib.sha256(content).hexdigest(),
            )
        except (KeyError, TypeError, ValueError, yaml.YAMLError) as error:
            raise CatalogConsistencyError(f"Invalid Registry manifest {key}: {error}") from error

    def _parse_file(self, manifest_key: str, value: object) -> PublishedFile:
        return self._files.parse_file(manifest_key, value)

    def _manifest_payload(
        self,
        snapshot: SourceSnapshot,
        files: tuple[tuple[PublicationFile, PublishedFile], ...],
        manifest_key: str,
        published_at: datetime,
    ) -> dict[str, Any]:
        extra = _publication_metadata(snapshot)
        return {
            "kind": "source_snapshot",
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "source": snapshot.dataset.source,
            "dataset": snapshot.dataset.dataset,
            "snapshot_id": snapshot.snapshot_id,
            "version": snapshot.version.value,
            "version_date": snapshot.version.version_date,
            "discovered_at": snapshot.version.discovered_at,
            "download_date": snapshot.downloaded_at.date(),
            "downloaded_at": snapshot.downloaded_at,
            "downloaded_by": "ifx-registry",
            "created_at": published_at,
            "upstream": {"homepage": snapshot.homepage, "urls": list(snapshot.upstream_urls)},
            "files": [
                {
                    "path": str(file.relative_path),
                    "size_bytes": file.size_bytes,
                    "sha256": file.sha256,
                    "content_type": file.content_type,
                    "source_url": file.source_url,
                    "storage_uri": file.storage_uri,
                }
                for _, file in files
            ],
            "extra": extra,
            "manifest_uri": self._uri(manifest_key),
        }

    def _manifest_key(self, dataset: DatasetId, version: str) -> str:
        return self._files.manifest_key(dataset, version)

    def _uri(self, key: str) -> str:
        return self._files.uri(key)


def _same_publication(
    published: PublishedSnapshot,
    snapshot: SourceSnapshot,
    candidates: tuple[tuple[PublicationFile, PublishedFile], ...],
) -> bool:
    expected_files = {
        (
            str(item.relative_path),
            item.size_bytes,
            item.sha256,
            item.content_type,
            item.source_url,
        )
        for item in published.files
    }
    actual_files = {
        (
            str(item.relative_path),
            item.size_bytes,
            item.sha256,
            item.content_type,
            item.source_url,
        )
        for _, item in candidates
    }
    return (
        published.dataset == snapshot.dataset
        and published.version.value == snapshot.version.value
        and published.version.version_date == snapshot.version.version_date
        and _same_json(published.version.evidence, snapshot.version.evidence)
        and published.homepage == snapshot.homepage
        and published.upstream_urls == snapshot.upstream_urls
        and _same_json(published.metadata, _publication_metadata(snapshot))
        and expected_files == actual_files
    )


def _publication_metadata(snapshot: SourceSnapshot) -> dict[str, Any]:
    extra = dict(snapshot.metadata)
    extra["version_method"] = {
        "type": snapshot.metadata.get("version_method"),
        "evidence": dict(snapshot.version.evidence),
    }
    return extra


def _same_json(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    try:
        return json.dumps(
            dict(left), sort_keys=True, separators=(",", ":"), allow_nan=False
        ) == json.dumps(dict(right), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return False


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        return {}
    return value


def _sequence(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise ValueError("manifest files must be a list")
    return tuple(value)


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("manifest timestamp is missing")
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None and str(value).strip() else None
