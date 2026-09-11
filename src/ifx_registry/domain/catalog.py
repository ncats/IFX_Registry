"""Domain objects presented by the Registry catalog."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    ExternalDatasetVersion,
    ProducerIdentity,
    SnapshotKind,
    SnapshotRef,
    SourceVersion,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """Human-facing description of one installed source adapter."""

    dataset: DatasetId
    display_name: str
    description: str
    expected_file_count: int
    homepage: str | None = None
    upstream_urls: tuple[str, ...] = ()
    version_check_description: str | None = None
    version_evidence_urls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError("source display_name must not be blank")
        if not self.description.strip():
            raise ValueError("source description must not be blank")
        if self.expected_file_count <= 0:
            raise ValueError("expected_file_count must be positive")
        if self.homepage is not None and not self.homepage.strip():
            raise ValueError("source homepage must not be blank")
        if self.version_check_description is not None and not (
            self.version_check_description.strip()
        ):
            raise ValueError("version_check_description must not be blank")
        if any(not url.strip() for url in self.upstream_urls):
            raise ValueError("source upstream_urls must not contain blank URLs")
        if any(not url.strip() for url in self.version_evidence_urls):
            raise ValueError("source version_evidence_urls must not contain blank URLs")


@dataclass(frozen=True, slots=True)
class PublishedFile:
    """Integrity and location metadata for one published artifact file."""

    relative_path: PurePosixPath
    storage_uri: str
    size_bytes: int
    sha256: str
    content_type: str | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        relative_path = PurePosixPath(self.relative_path)
        if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
            raise ValueError("published file path must be a safe relative POSIX path")
        if not self.storage_uri.startswith("s3://"):
            raise ValueError("published file storage_uri must be an S3 URI")
        if self.size_bytes < 0:
            raise ValueError("published file size must not be negative")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("published file sha256 must be 64 lowercase hexadecimal characters")
        object.__setattr__(self, "relative_path", relative_path)


@dataclass(frozen=True, slots=True)
class PublishedSnapshot:
    """An immutable source snapshot committed to the authoritative Registry."""

    dataset: DatasetId
    version: SourceVersion
    files: tuple[PublishedFile, ...]
    downloaded_at: datetime
    published_at: datetime
    manifest_uri: str
    homepage: str | None = None
    upstream_urls: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.files:
            raise ValueError("published snapshot must contain at least one file")
        if self.downloaded_at.tzinfo is None or self.published_at.tzinfo is None:
            raise ValueError("snapshot timestamps must be timezone-aware")
        if not self.manifest_uri.startswith("s3://"):
            raise ValueError("published snapshot manifest_uri must be an S3 URI")

    @property
    def snapshot_id(self) -> str:
        return f"{self.dataset}:{self.version}"

    @property
    def total_size_bytes(self) -> int:
        return sum(file.size_bytes for file in self.files)


@dataclass(frozen=True, slots=True)
class RegisteredSnapshotRef:
    """A verified dependency recorded in a derived snapshot manifest."""

    ref: SnapshotRef
    manifest_uri: str
    manifest_sha256: str | None = None
    slot: str | None = None

    def __post_init__(self) -> None:
        if self.slot is not None and not self.slot.strip():
            raise ValueError("registered dependency slot must not be blank")

    @property
    def kind(self) -> SnapshotKind:
        return self.ref.kind

    @property
    def snapshot_id(self) -> str:
        return self.ref.snapshot_id


@dataclass(frozen=True, slots=True)
class PublishedDerivedSnapshot:
    """An immutable caller-produced dataset committed to Registry storage."""

    dataset: DatasetId
    version: DatasetVersion
    files: tuple[PublishedFile, ...]
    inputs: tuple[RegisteredSnapshotRef, ...]
    producer: ProducerIdentity | None
    transform: Mapping[str, Any]
    validation: Mapping[str, Any]
    published_at: datetime
    manifest_uri: str
    build_key: str | None
    publication_fingerprint: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.files:
            raise ValueError("published derived snapshot must contain at least one file")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        if not self.manifest_uri.startswith("s3://"):
            raise ValueError("published derived snapshot manifest_uri must be an S3 URI")
        if self.build_key is not None and not self.build_key.strip():
            raise ValueError("derived snapshot build_key must not be blank")
        if not _SHA256.fullmatch(self.publication_fingerprint):
            raise ValueError("publication_fingerprint must be a SHA-256 digest")

    @property
    def snapshot_id(self) -> str:
        return f"{self.dataset}:{self.version}"

    @property
    def total_size_bytes(self) -> int:
        return sum(file.size_bytes for file in self.files)


PublishedDatasetSnapshot = PublishedSnapshot | PublishedDerivedSnapshot


@dataclass(frozen=True, slots=True)
class PublishedExternalDatasetVersion:
    """One immutable, sanitized metadata-only external version assertion."""

    value: ExternalDatasetVersion
    published_at: datetime
    manifest_uri: str
    manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None:
            raise ValueError("external published_at must be timezone-aware")
        if not self.manifest_uri.startswith("s3://"):
            raise ValueError("external manifest_uri must be an S3 URI")

    @property
    def dataset(self) -> DatasetId:
        return self.value.dataset

    @property
    def version(self) -> DatasetVersion:
        return self.value.version

    @property
    def snapshot_id(self) -> str:
        return self.value.snapshot_id
