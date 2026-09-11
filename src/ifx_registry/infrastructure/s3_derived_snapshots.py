"""Manifest-backed derived dataset catalog and publisher for S3."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from ifx_registry.application.ports.snapshots import (
    DerivedSnapshotPublisher,
    PublishedDerivedSnapshotCatalog,
    PublishedFileReader,
)
from ifx_registry.domain.catalog import (
    PublishedDerivedSnapshot,
    PublishedFile,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.derived_builds import (
    derived_publication_fingerprint,
    registered_input_payload,
    registered_input_sort_key,
)
from ifx_registry.domain.errors import (
    CatalogConsistencyError,
    SnapshotAlreadyExistsError,
    SnapshotNotFoundError,
)
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    DerivedSnapshot,
    ProducerIdentity,
    SnapshotKind,
    SnapshotRef,
)
from ifx_registry.infrastructure.object_store import ObjectStore
from ifx_registry.infrastructure.s3_dataset_files import (
    PublicationFile,
    S3DatasetFileStore,
)

MANIFEST_SCHEMA_VERSION = 1


class S3DerivedSnapshotRepository(
    PublishedDerivedSnapshotCatalog,
    DerivedSnapshotPublisher,
    PublishedFileReader,
):
    """Read and atomically publish canonical derived-snapshot manifests."""

    def __init__(self, objects: ObjectStore, *, prefix: str = ""):
        self._files = S3DatasetFileStore(objects, "derived", prefix=prefix)

    def list_all(self) -> tuple[PublishedDerivedSnapshot, ...]:
        snapshots = [self._load_manifest(key) for key in self._files.list_manifest_keys()]
        return tuple(sorted(snapshots, key=lambda item: item.published_at, reverse=True))

    def get(self, dataset: DatasetId, version: str) -> PublishedDerivedSnapshot:
        normalized = DatasetVersion(version).value
        key = self._files.manifest_key(dataset, normalized)
        payload = self._files.read_manifest(key)
        if payload is None:
            raise SnapshotNotFoundError(
                f"Published derived snapshot not found: {dataset}:{version}"
            )
        return self._parse_manifest(key, payload)

    def download(self, file: PublishedFile, destination: Path) -> None:
        self._files.download(file, destination)

    def publish(
        self,
        snapshot: DerivedSnapshot,
        inputs: tuple[RegisteredSnapshotRef, ...],
    ) -> PublishedDerivedSnapshot:
        manifest_key = self._files.manifest_key(snapshot.dataset, snapshot.version.value)
        candidates = self._files.inspect_files(
            snapshot.dataset,
            snapshot.version.value,
            tuple(
                PublicationFile(
                    file.local_path,
                    file.relative_path,
                    file.content_type,
                    f"derived://{snapshot.snapshot_id}/{file.relative_path}",
                )
                for file in snapshot.files
            ),
        )
        existing_payload = self._files.read_manifest(manifest_key)
        if existing_payload is not None:
            existing = self._parse_manifest(manifest_key, existing_payload)
            if _same_publication(existing, snapshot, inputs, candidates):
                self._files.verify_files(existing.files)
                return existing
            raise SnapshotAlreadyExistsError(
                "Published derived snapshot is immutable and has different provenance "
                f"or files: {snapshot.snapshot_id}"
            )

        self._files.ensure_files(candidates)
        published_at = datetime.now(UTC)
        payload = self._manifest_payload(snapshot, inputs, candidates, published_at)
        manifest_bytes = yaml.safe_dump(payload, sort_keys=False).encode("utf-8")
        if not self._files.commit_manifest(manifest_key, manifest_bytes):
            winner_payload = self._files.read_manifest(manifest_key)
            if winner_payload is None:
                raise CatalogConsistencyError(
                    f"Manifest commit was rejected but no manifest exists: {manifest_key}"
                )
            winner = self._parse_manifest(manifest_key, winner_payload)
            if _same_publication(winner, snapshot, inputs, candidates):
                self._files.verify_files(winner.files)
                return winner
            raise SnapshotAlreadyExistsError(
                "Derived snapshot was committed concurrently with different provenance "
                f"or files: {snapshot.snapshot_id}"
            )
        committed = self._load_manifest(manifest_key)
        self._files.verify_files(committed.files)
        return committed

    def _load_manifest(self, key: str) -> PublishedDerivedSnapshot:
        payload = self._files.read_manifest(key)
        if payload is None:
            raise SnapshotNotFoundError(f"Published manifest not found: {key}")
        return self._parse_manifest(key, payload)

    def _parse_manifest(self, key: str, content: bytes) -> PublishedDerivedSnapshot:
        try:
            payload = yaml.safe_load(content)
            if not isinstance(payload, dict):
                raise ValueError("manifest root must be a mapping")
            if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
                raise ValueError("unsupported schema_version")
            if payload.get("kind") != SnapshotKind.DERIVED.value:
                raise ValueError("manifest kind is not derived_snapshot")
            dataset = DatasetId(str(payload["source"]), str(payload["dataset"]))
            version_value = str(payload["version"])
            version = DatasetVersion(
                version_value,
                version_date=_optional_date(
                    payload.get("version_date"), legacy_version=version_value
                ),
            )
            if key != self._files.manifest_key(dataset, version.value):
                raise ValueError("manifest identity does not match its S3 key")
            snapshot_id = f"{dataset}:{version}"
            if payload.get("snapshot_id", snapshot_id) != snapshot_id:
                raise ValueError("manifest snapshot_id does not match its identity")
            inputs = tuple(
                sorted(
                    (_registered_ref(item) for item in _sequence(payload["derived_from"])),
                    key=registered_input_sort_key,
                )
            )
            producer = _producer(payload.get("producer"))
            transform = _mapping(payload.get("transform"))
            validation = _mapping(payload.get("validation") or payload.get("stats"))
            build_key = _optional_string(payload.get("build_key"))
            publication_fingerprint = str(
                payload.get("publication_fingerprint")
                or derived_publication_fingerprint(inputs, producer, transform)
            )
            files = tuple(
                self._files.parse_file(key, item) for item in _sequence(payload["files"])
            )
            return PublishedDerivedSnapshot(
                dataset=dataset,
                version=version,
                files=files,
                inputs=inputs,
                producer=producer,
                transform=transform,
                validation=validation,
                published_at=_datetime(payload.get("created_at")),
                manifest_uri=self._files.uri(key),
                manifest_sha256=hashlib.sha256(content).hexdigest(),
                build_key=build_key,
                publication_fingerprint=publication_fingerprint,
                metadata=_mapping(payload.get("extra") or payload.get("metadata")),
            )
        except (KeyError, TypeError, ValueError, yaml.YAMLError) as error:
            raise CatalogConsistencyError(f"Invalid Registry manifest {key}: {error}") from error

    def _manifest_payload(
        self,
        snapshot: DerivedSnapshot,
        inputs: tuple[RegisteredSnapshotRef, ...],
        candidates: tuple[tuple[PublicationFile, PublishedFile], ...],
        published_at: datetime,
    ) -> dict[str, Any]:
        producer = snapshot.producer
        return {
            "kind": SnapshotKind.DERIVED.value,
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "source": snapshot.dataset.source,
            "dataset": snapshot.dataset.dataset,
            "snapshot_id": snapshot.snapshot_id,
            "version": snapshot.version.value,
            "version_date": snapshot.version.version_date,
            "created_at": published_at,
            "derived_from": [
                _input_payload(item)
                for item in sorted(inputs, key=registered_input_sort_key)
            ],
            "producer": {
                "name": producer.name,
                "release": producer.release,
                "code_repository": producer.code_repository,
                "code_revision": producer.code_revision,
            },
            "transform": dict(snapshot.transform),
            "validation": dict(snapshot.validation),
            "stats": dict(snapshot.validation),
            "build_key": derived_publication_fingerprint(inputs, producer, snapshot.transform),
            "publication_fingerprint": derived_publication_fingerprint(
                inputs, producer, snapshot.transform
            ),
            "files": [
                {
                    "path": str(file.relative_path),
                    "size_bytes": file.size_bytes,
                    "sha256": file.sha256,
                    "content_type": file.content_type,
                    "source_url": file.source_url,
                    "storage_uri": file.storage_uri,
                }
                for _, file in candidates
            ],
            "extra": dict(snapshot.metadata),
            "manifest_uri": self._files.uri(
                self._files.manifest_key(snapshot.dataset, snapshot.version.value)
            ),
        }


def _same_publication(
    published: PublishedDerivedSnapshot,
    snapshot: DerivedSnapshot,
    inputs: tuple[RegisteredSnapshotRef, ...],
    candidates: tuple[tuple[PublicationFile, PublishedFile], ...],
) -> bool:
    expected_files = {
        (
            str(file.relative_path),
            file.size_bytes,
            file.sha256,
            file.content_type,
        )
        for file in published.files
    }
    actual_files = {
        (
            str(file.relative_path),
            file.size_bytes,
            file.sha256,
            file.content_type,
        )
        for _, file in candidates
    }
    return (
        published.dataset == snapshot.dataset
        and published.version == snapshot.version
        and published.inputs == tuple(sorted(inputs, key=registered_input_sort_key))
        and published.producer == snapshot.producer
        and _same_json(published.transform, snapshot.transform)
        and _same_json(published.validation, snapshot.validation)
        and _same_json(published.metadata, snapshot.metadata)
        and published.publication_fingerprint
        == derived_publication_fingerprint(inputs, snapshot.producer, snapshot.transform)
        and expected_files == actual_files
    )


def _same_json(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    try:
        return json.dumps(
            dict(left), sort_keys=True, separators=(",", ":"), allow_nan=False
        ) == json.dumps(dict(right), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return False


def _input_payload(value: RegisteredSnapshotRef) -> dict[str, Any]:
    return registered_input_payload(value)


def _registered_ref(value: object) -> RegisteredSnapshotRef:
    item = _mapping(value)
    aliases = {
        "source": SnapshotKind.SOURCE,
        SnapshotKind.SOURCE.value: SnapshotKind.SOURCE,
        "derived": SnapshotKind.DERIVED,
        SnapshotKind.DERIVED.value: SnapshotKind.DERIVED,
        "external": SnapshotKind.EXTERNAL,
        SnapshotKind.EXTERNAL.value: SnapshotKind.EXTERNAL,
    }
    kind_value = item.get("kind")
    if kind_value is None:
        manifest_uri = str(item.get("manifest_uri") or "")
        if "/derived/" in manifest_uri:
            kind = SnapshotKind.DERIVED
        elif "/sources/" in manifest_uri:
            kind = SnapshotKind.SOURCE
        elif "/external/" in manifest_uri:
            kind = SnapshotKind.EXTERNAL
        else:
            raise ValueError("legacy dependency kind cannot be inferred from manifest_uri")
    else:
        kind_text = str(kind_value)
        try:
            kind = aliases[kind_text]
        except KeyError as error:
            raise ValueError(f"unsupported dependency kind: {kind_text}") from error
    snapshot_id = item.get("snapshot_id")
    if not snapshot_id:
        snapshot_id = f"{item['source']}:{item['dataset']}:{item['version']}"
    factories = {
        SnapshotKind.SOURCE: SnapshotRef.source,
        SnapshotKind.DERIVED: SnapshotRef.derived,
        SnapshotKind.EXTERNAL: SnapshotRef.external,
    }
    ref = factories[kind](str(snapshot_id))
    return RegisteredSnapshotRef(
        ref=ref,
        manifest_uri=str(item.get("manifest_uri") or ""),
        manifest_sha256=_optional_string(item.get("manifest_sha256")),
        slot=_optional_string(item.get("slot")),
    )


def _producer(value: object) -> ProducerIdentity | None:
    item = _mapping(value)
    if not item:
        return None
    return ProducerIdentity(
        name=str(item["name"]),
        release=str(item["release"]),
        code_repository=str(item["code_repository"]),
        code_revision=str(item["code_revision"]),
    )


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        return {}
    return value


def _sequence(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise ValueError("manifest value must be a list")
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


def _optional_date(value: object, *, legacy_version: str | None = None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        # Early Registry writers sometimes copied a dependency-derived version
        # label into this optional display field. The key and `version` remain
        # authoritative, so preserve compatibility by treating the date as unknown.
        if str(value) == legacy_version and str(value).startswith("deps-"):
            return None
        raise


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None
