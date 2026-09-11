"""Framework-independent results returned by Registry use cases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar

from ifx_registry.domain.catalog import (
    PublishedDatasetSnapshot,
    PublishedDerivedSnapshot,
    PublishedExternalDatasetVersion,
    PublishedFile,
    PublishedSnapshot,
    RegisteredSnapshotRef,
)
from ifx_registry.domain.errors import DatasetFileNotFoundError, MultipleDatasetFilesError
from ifx_registry.domain.models import ProducerIdentity

SnapshotT = TypeVar("SnapshotT", PublishedSnapshot, PublishedDerivedSnapshot)


@dataclass(frozen=True, slots=True)
class _DatasetDescription(Generic[SnapshotT]):
    """Shared metadata for one exact snapshot without materializing it."""

    snapshot: SnapshotT

    @property
    def kind(self) -> str:
        return (
            "derived_snapshot"
            if isinstance(self.snapshot, PublishedDerivedSnapshot)
            else "source_snapshot"
        )

    @property
    def source(self) -> str:
        return self.snapshot.dataset.source

    @property
    def dataset(self) -> str:
        return self.snapshot.dataset.dataset

    @property
    def version(self) -> str:
        return self.snapshot.version.value

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id

    @property
    def version_date(self) -> date | None:
        return self.snapshot.version.version_date

    @property
    def registered_at(self) -> datetime:
        return self.snapshot.published_at

    @property
    def manifest_uri(self) -> str:
        return self.snapshot.manifest_uri

    @property
    def files(self) -> tuple[PublishedFile, ...]:
        return self.snapshot.files

    @property
    def file_names(self) -> tuple[str, ...]:
        return tuple(str(file.relative_path) for file in self.files)

    @property
    def total_size_bytes(self) -> int:
        return self.snapshot.total_size_bytes

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a JSON/YAML-safe representation of the registered manifest."""
        return _manifest_dict(self.snapshot)


@dataclass(frozen=True, slots=True)
class DatasetDescription(_DatasetDescription[PublishedSnapshot]):
    """Metadata for one exact source snapshot, without downloading it."""

    @property
    def downloaded_at(self) -> datetime:
        return self.snapshot.downloaded_at


@dataclass(frozen=True, slots=True)
class DerivedDatasetDescription(_DatasetDescription[PublishedDerivedSnapshot]):
    """Metadata and typed lineage for one exact derived snapshot."""

    @property
    def downloaded_at(self) -> None:
        return None

    @property
    def inputs(self) -> tuple[RegisteredSnapshotRef, ...]:
        return self.snapshot.inputs

    @property
    def input_ids(self) -> tuple[str, ...]:
        return tuple(item.snapshot_id for item in self.inputs)

    @property
    def producer(self) -> ProducerIdentity | None:
        return self.snapshot.producer

    @property
    def transform(self) -> Mapping[str, Any]:
        return self.snapshot.transform

    @property
    def validation(self) -> Mapping[str, Any]:
        return self.snapshot.validation


@dataclass(frozen=True, slots=True)
class ExternalDatasetDescription:
    """Sanitized metadata for one exact external dataset version."""

    record: PublishedExternalDatasetVersion

    @property
    def kind(self) -> str:
        return "external_dataset_version"

    @property
    def source(self) -> str:
        return self.record.dataset.source

    @property
    def dataset(self) -> str:
        return self.record.dataset.dataset

    @property
    def version(self) -> str:
        return self.record.version.value

    @property
    def version_date(self) -> date | None:
        return self.record.version.version_date

    @property
    def snapshot_id(self) -> str:
        return self.record.snapshot_id

    @property
    def registered_at(self) -> datetime:
        return self.record.published_at

    @property
    def observed_at(self) -> datetime:
        return self.record.value.observed_at

    @property
    def interface(self) -> str:
        return self.record.value.interface

    @property
    def access_mode(self) -> str:
        return self.record.value.access_mode

    @property
    def service_name(self) -> str:
        return self.record.value.service_name

    @property
    def documentation_url(self) -> str | None:
        return self.record.value.documentation_url

    @property
    def version_check(self) -> Mapping[str, Any]:
        return self.record.value.version_check

    @property
    def version_evidence(self) -> Mapping[str, Any]:
        return self.record.value.version_evidence

    @property
    def metadata(self) -> Mapping[str, Any]:
        return self.record.value.metadata

    @property
    def manifest_uri(self) -> str:
        return self.record.manifest_uri

    @property
    def manifest(self) -> dict[str, Any]:
        return {
            "kind": "external_source_registration",
            "schema_version": 1,
            "source": self.source,
            "dataset": self.dataset,
            "registration_id": self.snapshot_id,
            "version": self.version,
            "version_date": iso_date(self.version_date),
            "observed_at": self.observed_at.isoformat(),
            "created_at": self.registered_at.isoformat(),
            "interface": self.interface,
            "access_mode": self.access_mode,
            "service_name": self.service_name,
            "documentation_url": self.documentation_url,
            "version_check": _json_safe(self.version_check),
            "version_evidence": _json_safe(self.version_evidence),
            "metadata": _json_safe(self.metadata),
            "manifest_uri": self.manifest_uri,
            "manifest_sha256": self.record.manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class _MaterializedDataset(Generic[SnapshotT]):
    """Shared behavior for a snapshot verified in a caller-owned cache."""

    snapshot: SnapshotT
    local_dir: Path

    @property
    def download_date(self) -> date:
        raise NotImplementedError

    @property
    def source(self) -> str:
        return self.snapshot.dataset.source

    @property
    def dataset(self) -> str:
        return self.snapshot.dataset.dataset

    @property
    def version(self) -> str:
        return self.snapshot.version.value

    @property
    def version_date(self) -> date | None:
        return self.snapshot.version.version_date

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id

    @property
    def manifest_uri(self) -> str:
        return self.snapshot.manifest_uri

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a JSON/YAML-safe representation of the registered manifest."""
        return _manifest_dict(self.snapshot)

    def file(self, file_name: str | None = None) -> Path:
        if file_name is None:
            if len(self.snapshot.files) != 1:
                raise MultipleDatasetFilesError(
                    f"Dataset {self.snapshot_id} has {len(self.snapshot.files)} files; "
                    f"choose one of: {', '.join(self.files)}"
                )
            relative_path = self.snapshot.files[0].relative_path
        else:
            match = next(
                (
                    file.relative_path
                    for file in self.snapshot.files
                    if str(file.relative_path) == file_name
                ),
                None,
            )
            if match is None:
                raise DatasetFileNotFoundError(
                    f"Dataset {self.snapshot_id} does not declare {file_name!r}; "
                    f"choose one of: {', '.join(self.files)}"
                )
            relative_path = match
        path = self.local_dir / relative_path
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    @property
    def files(self) -> dict[str, Path]:
        """Map every declared snapshot file name to its verified local path."""

        return {
            str(file.relative_path): self.local_dir / file.relative_path
            for file in self.snapshot.files
        }

    def to_metadata(self) -> dict[str, Any]:
        metadata = {
            "kind": (
                "derived_snapshot"
                if isinstance(self.snapshot, PublishedDerivedSnapshot)
                else "source_snapshot"
            ),
            "source": self.source,
            "dataset": self.dataset,
            "version": self.version,
            "version_date": iso_date(self.version_date),
            "download_date": iso_date(self.download_date),
            "snapshot_id": self.snapshot_id,
            "manifest_uri": self.manifest_uri,
            "local_dir": str(self.local_dir),
            "files": self.manifest["files"],
        }
        if isinstance(self.snapshot, PublishedDerivedSnapshot):
            metadata["derived_from"] = self.manifest["derived_from"]
            metadata["producer"] = self.manifest["producer"]
            metadata["transform"] = self.manifest["transform"]
            metadata["validation"] = self.manifest["validation"]
            metadata["build_key"] = self.snapshot.build_key
            metadata["publication_fingerprint"] = self.snapshot.publication_fingerprint
        return metadata


@dataclass(frozen=True, slots=True)
class MaterializedDataset(_MaterializedDataset[PublishedSnapshot]):
    """One exact source snapshot verified in a caller-owned cache."""

    @property
    def download_date(self) -> date:
        return self.snapshot.downloaded_at.date()


@dataclass(frozen=True, slots=True)
class DerivedMaterializedDataset(_MaterializedDataset[PublishedDerivedSnapshot]):
    """One exact derived snapshot verified in a caller-owned cache."""

    @property
    def download_date(self) -> date:
        return self.snapshot.published_at.date()


def iso_date(value: date | datetime | None) -> str | None:
    """Return a stable date string for compatibility boundaries."""
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    return value.isoformat()


def _manifest_dict(snapshot: PublishedDatasetSnapshot) -> dict[str, Any]:
    if isinstance(snapshot, PublishedDerivedSnapshot):
        return _derived_manifest_dict(snapshot)
    return {
        "kind": "source_snapshot",
        "schema_version": 1,
        "source": snapshot.dataset.source,
        "dataset": snapshot.dataset.dataset,
        "snapshot_id": snapshot.snapshot_id,
        "version": snapshot.version.value,
        "version_date": iso_date(snapshot.version.version_date),
        "download_date": iso_date(snapshot.downloaded_at),
        "downloaded_at": snapshot.downloaded_at.isoformat(),
        "created_at": snapshot.published_at.isoformat(),
        "upstream": {
            "homepage": snapshot.homepage,
            "urls": list(snapshot.upstream_urls),
        },
        "files": [
            {
                "path": str(file.relative_path),
                "storage_uri": file.storage_uri,
                "size_bytes": file.size_bytes,
                "sha256": file.sha256,
                "content_type": file.content_type,
                "source_url": file.source_url,
            }
            for file in snapshot.files
        ],
        "extra": _json_safe(snapshot.metadata),
        "manifest_uri": snapshot.manifest_uri,
        "manifest_sha256": snapshot.manifest_sha256,
    }


def _derived_manifest_dict(snapshot: PublishedDerivedSnapshot) -> dict[str, Any]:
    producer = snapshot.producer
    return {
        "kind": "derived_snapshot",
        "schema_version": 1,
        "source": snapshot.dataset.source,
        "dataset": snapshot.dataset.dataset,
        "snapshot_id": snapshot.snapshot_id,
        "version": snapshot.version.value,
        "version_date": iso_date(snapshot.version.version_date),
        "created_at": snapshot.published_at.isoformat(),
        "derived_from": [
            {
                "kind": item.kind.value,
                "snapshot_id": item.snapshot_id,
                "source": item.ref.dataset.source,
                "dataset": item.ref.dataset.dataset,
                "version": item.ref.version.value,
                "manifest_uri": item.manifest_uri,
                "manifest_sha256": item.manifest_sha256,
            }
            for item in snapshot.inputs
        ],
        "producer": (
            {
                "name": producer.name,
                "release": producer.release,
                "code_repository": producer.code_repository,
                "code_revision": producer.code_revision,
            }
            if producer is not None
            else None
        ),
        "transform": _json_safe(snapshot.transform),
        "validation": _json_safe(snapshot.validation),
        "build_key": snapshot.build_key,
        "publication_fingerprint": snapshot.publication_fingerprint,
        "files": [
            {
                "path": str(file.relative_path),
                "storage_uri": file.storage_uri,
                "size_bytes": file.size_bytes,
                "sha256": file.sha256,
                "content_type": file.content_type,
                "source_url": file.source_url,
            }
            for file in snapshot.files
        ],
        "extra": _json_safe(snapshot.metadata),
        "manifest_uri": snapshot.manifest_uri,
        "manifest_sha256": snapshot.manifest_sha256,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
