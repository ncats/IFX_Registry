"""Small public client for consuming pinned Registry source snapshots."""

from __future__ import annotations

import json
import mimetypes
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from ifx_registry.application.models import (
    DatasetDescription,
    DerivedDatasetDescription,
    DerivedMaterializedDataset,
    ExternalDatasetDescription,
    MaterializedDataset,
)
from ifx_registry.application.use_cases.derived_datasets import (
    DescribeDerivedDataset,
    MaterializeDerivedDataset,
    MaterializeDerivedFile,
    PublishDerivedDataset,
)
from ifx_registry.application.use_cases.describe_dataset import DescribeDataset
from ifx_registry.application.use_cases.external_versions import (
    DescribeExternalDatasetVersion,
    ListExternalDatasetVersions,
    RegisterExternalDatasetVersion,
)
from ifx_registry.application.use_cases.materialize_dataset import MaterializeDataset
from ifx_registry.application.use_cases.materialize_file import MaterializeFile
from ifx_registry.application.use_cases.publish_source_dataset import PublishSourceDataset
from ifx_registry.domain.errors import (
    InvalidPublicationError,
    InvalidSnapshotIdError,
    RegistryClientConfigurationError,
)
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    DerivedSnapshot,
    DerivedSnapshotFile,
    ExternalDatasetVersion,
    ProducerIdentity,
    SnapshotFile,
    SnapshotRef,
    SourceSnapshot,
    SourceVersion,
)
from ifx_registry.infrastructure.aws_credentials import load_aws_credentials
from ifx_registry.infrastructure.materialization import FileSystemSnapshotMaterializer
from ifx_registry.infrastructure.object_store import Boto3ObjectStore
from ifx_registry.infrastructure.s3_derived_snapshots import S3DerivedSnapshotRepository
from ifx_registry.infrastructure.s3_external_versions import S3ExternalDatasetVersionRepository
from ifx_registry.infrastructure.s3_snapshots import S3PublishedSnapshotRepository

DEFAULT_CACHE_DIR = Path(os.environ.get("IFX_REGISTRY_CACHE_DIR", "/var/tmp/ifx-registry-cache"))


class RegistryClient:
    """Caller-oriented facade for exact-version source materialization."""

    def __init__(
        self,
        materialize_dataset: MaterializeDataset,
        materialize_file: MaterializeFile,
        *,
        describe_dataset: DescribeDataset | None = None,
        derived_client: DerivedRegistryClient | None = None,
        external_client: ExternalRegistryClient | None = None,
        publish_source: PublishSourceDataset | None = None,
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
    ):
        self._materialize_dataset = materialize_dataset
        self._materialize_file = materialize_file
        self._describe_dataset = describe_dataset
        self._derived_client = derived_client
        self._external_client = external_client
        self._publish_source = publish_source
        self._cache_dir = Path(cache_dir)

    @classmethod
    def connect(
        cls,
        credentials_file: str | Path | None = None,
        *,
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
        bucket: str | None = None,
        region: str | None = None,
        prefix: str = "",
    ) -> RegistryClient:
        """Connect through a credential YAML or the standard AWS credential chain."""

        if credentials_file is not None:
            return cls.from_credentials(
                credentials_file,
                cache_dir=cache_dir,
                bucket=bucket,
                region=region,
                prefix=prefix,
            )
        return cls.from_aws(
            bucket=bucket or "aws-ifx-registry",
            region=region or "us-east-1",
            prefix=prefix,
            cache_dir=cache_dir,
        )

    @classmethod
    def from_aws(
        cls,
        *,
        bucket: str = "aws-ifx-registry",
        region: str = "us-east-1",
        prefix: str = "",
        endpoint_url: str | None = None,
        role_arn: str | None = None,
        external_id: str | None = None,
        role_session_name: str = "ifx-registry",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
    ) -> RegistryClient:
        objects = Boto3ObjectStore(
            bucket,
            region=region,
            endpoint_url=endpoint_url,
            role_arn=role_arn,
            external_id=external_id,
            role_session_name=role_session_name,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )
        snapshots = S3PublishedSnapshotRepository(objects, prefix=prefix)
        derived_snapshots = S3DerivedSnapshotRepository(objects, prefix=prefix)
        external_versions = S3ExternalDatasetVersionRepository(objects, prefix=prefix)
        materializer = FileSystemSnapshotMaterializer(snapshots)
        derived_materializer = FileSystemSnapshotMaterializer(derived_snapshots)
        return cls(
            MaterializeDataset(snapshots, materializer),
            MaterializeFile(snapshots, materializer),
            describe_dataset=DescribeDataset(snapshots),
            derived_client=DerivedRegistryClient(
                PublishDerivedDataset(
                    snapshots,
                    derived_snapshots,
                    external_versions,
                    derived_snapshots,
                ),
                DescribeDerivedDataset(derived_snapshots),
                MaterializeDerivedDataset(derived_snapshots, derived_materializer),
                MaterializeDerivedFile(derived_snapshots, derived_materializer),
                cache_dir=cache_dir,
            ),
            external_client=ExternalRegistryClient(
                RegisterExternalDatasetVersion(external_versions),
                DescribeExternalDatasetVersion(external_versions),
                ListExternalDatasetVersions(external_versions),
            ),
            publish_source=PublishSourceDataset(snapshots),
            cache_dir=cache_dir,
        )

    @classmethod
    def from_credentials(
        cls,
        path: str | Path,
        *,
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
        bucket: str | None = None,
        region: str | None = None,
        prefix: str = "",
    ) -> RegistryClient:
        """Connect using the team's Registry credential YAML."""

        return cls.from_aws_credentials(
            path,
            cache_dir=cache_dir,
            bucket=bucket,
            region=region,
            prefix=prefix,
        )

    @classmethod
    def from_aws_credentials(
        cls,
        path: str | Path,
        *,
        bucket: str | None = None,
        region: str | None = None,
        prefix: str = "",
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
    ) -> RegistryClient:
        credentials = load_aws_credentials(Path(path))
        return cls.from_aws(
            bucket=bucket or credentials.bucket,
            region=region or credentials.region,
            prefix=prefix,
            endpoint_url=credentials.endpoint_url,
            role_arn=credentials.role_arn,
            external_id=credentials.external_id,
            role_session_name=credentials.session_name,
            access_key_id=credentials.access_key_id,
            secret_access_key=credentials.secret_access_key,
            cache_dir=cache_dir,
        )

    def materialize(
        self,
        snapshot_id: str,
        *,
        destination: str | Path | None = None,
    ) -> MaterializedDataset:
        dataset, version = _parse_snapshot_id(snapshot_id)
        return self._materialize_dataset.execute(
            dataset,
            version,
            destination=Path(destination) if destination is not None else self._cache_dir,
        )

    def describe(self, snapshot_id: str) -> DatasetDescription:
        """Return filenames, sizes, checksums, and provenance without downloading files."""
        dataset, version = _parse_snapshot_id(snapshot_id)
        if self._describe_dataset is None:
            raise RegistryClientConfigurationError(
                "This manually constructed RegistryClient has no metadata query; "
                "provide describe_dataset= or use RegistryClient.connect()"
            )
        return self._describe_dataset.execute(dataset, version)

    @property
    def cache_dir(self) -> Path:
        """Directory used for verified local copies."""

        return self._cache_dir

    @property
    def derived(self) -> DerivedRegistryClient:
        """Explicit interface for caller-produced derived datasets."""
        if self._derived_client is None:
            raise RegistryClientConfigurationError(
                "This manually constructed RegistryClient has no derived interface; "
                "provide derived_client= or use RegistryClient.connect()"
            )
        return self._derived_client

    @property
    def external(self) -> ExternalRegistryClient:
        """Explicit interface for metadata-only external dataset versions."""
        if self._external_client is None:
            raise RegistryClientConfigurationError(
                "This manually constructed RegistryClient has no external interface; "
                "provide external_client= or use RegistryClient.connect()"
            )
        return self._external_client

    def publish_source(
        self,
        snapshot_id: str,
        *,
        files: Mapping[str, str | Path],
        captured_at: datetime,
        capture_method: Literal["manual", "provider_export"],
        version_date: date | None = None,
        file_sources: Mapping[str, str] | None = None,
        homepage: str | None = None,
        upstream_urls: Sequence[str] = (),
        version_evidence: Mapping[str, Any] | None = None,
        validation: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> DatasetDescription:
        """Publish faithful caller-captured source files without an installed adapter."""
        if self._publish_source is None:
            raise RegistryClientConfigurationError(
                "This manually constructed RegistryClient cannot publish sources; "
                "provide publish_source= or use RegistryClient.connect()"
            )
        if capture_method not in {"manual", "provider_export"}:
            raise InvalidPublicationError(
                "capture_method must be 'manual' or 'provider_export'"
            )
        if captured_at.tzinfo is None:
            raise InvalidPublicationError("captured_at must be timezone-aware")
        dataset, version_value = _parse_snapshot_id(snapshot_id)
        file_sources = dict(file_sources or {})
        unknown_sources = set(file_sources) - set(files)
        if unknown_sources:
            raise InvalidPublicationError(
                "file_sources contains names not present in files: "
                + ", ".join(sorted(unknown_sources))
            )
        evidence = _json_mapping(version_evidence or {}, "version_evidence")
        validation_data = _json_mapping(validation or {}, "validation")
        metadata_data = _json_mapping(metadata or {}, "metadata")
        metadata_data.update(
            {"capture_method": capture_method, "validation": validation_data}
        )
        snapshot = SourceSnapshot(
            dataset=dataset,
            version=SourceVersion(
                version_value,
                version_date=version_date,
                discovered_at=captured_at,
                evidence=evidence,
            ),
            files=tuple(
                SnapshotFile(
                    local_path=Path(local_path),
                    relative_path=PurePosixPath(relative_path),
                    source_url=file_sources.get(relative_path),
                    content_type=mimetypes.guess_type(relative_path)[0],
                )
                for relative_path, local_path in sorted(files.items())
            ),
            downloaded_at=captured_at,
            homepage=homepage,
            upstream_urls=tuple(upstream_urls),
            metadata=metadata_data,
        )
        return self._publish_source.execute(snapshot)

    def file(
        self,
        snapshot_id: str,
        file_name: str | None = None,
        *,
        destination: str | Path | None = None,
    ) -> Path:
        """Return a verified local file from one exact Registry version."""

        dataset, version = _parse_snapshot_id(snapshot_id)
        return self._materialize_file.execute(
            dataset,
            version,
            file_name,
            destination=Path(destination) if destination is not None else self._cache_dir,
        )

    def files(
        self,
        snapshot_id: str,
        *,
        destination: str | Path | None = None,
    ) -> dict[str, Path]:
        """Return every verified local file in one exact Registry version."""

        return self.materialize(snapshot_id, destination=destination).files


def _parse_snapshot_id(snapshot_id: str) -> tuple[DatasetId, str]:
    parts = snapshot_id.split(":")
    if len(parts) != 3 or any(not part for part in parts):
        raise InvalidSnapshotIdError(
            f"Pinned snapshot must be source:dataset:version, got {snapshot_id!r}"
        )
    try:
        dataset = DatasetId(parts[0], parts[1])
        version = SourceVersion(parts[2]).value
    except ValueError as error:
        raise InvalidSnapshotIdError(
            f"Pinned snapshot must be source:dataset:version, got {snapshot_id!r}"
        ) from error
    return dataset, version


class DerivedRegistryClient:
    """Publish and consume immutable caller-produced datasets."""

    def __init__(
        self,
        publish_dataset: PublishDerivedDataset,
        describe_dataset: DescribeDerivedDataset,
        materialize_dataset: MaterializeDerivedDataset,
        materialize_file: MaterializeDerivedFile,
        *,
        cache_dir: str | Path = DEFAULT_CACHE_DIR,
    ):
        self._publish_dataset = publish_dataset
        self._describe_dataset = describe_dataset
        self._materialize_dataset = materialize_dataset
        self._materialize_file = materialize_file
        self._cache_dir = Path(cache_dir)

    def publish(
        self,
        snapshot_id: str,
        *,
        files: Mapping[str, str | Path],
        inputs: Sequence[SnapshotRef],
        producer: ProducerIdentity,
        transform: Mapping[str, Any],
        validation: Mapping[str, Any],
        version_date: date | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> DerivedDatasetDescription:
        """Register caller-owned files without moving or deleting them."""
        dataset, version_value = _parse_snapshot_id(snapshot_id)
        derived_files = tuple(
            DerivedSnapshotFile(
                local_path=Path(local_path),
                relative_path=PurePosixPath(relative_path),
                content_type=mimetypes.guess_type(relative_path)[0],
            )
            for relative_path, local_path in sorted(files.items())
        )
        snapshot = DerivedSnapshot(
            dataset=dataset,
            version=DatasetVersion(version_value, version_date=version_date),
            files=derived_files,
            inputs=tuple(inputs),
            producer=producer,
            transform=transform,
            validation=validation,
            metadata=metadata or {},
        )
        return self._publish_dataset.execute(snapshot)

    def describe(self, snapshot_id: str) -> DerivedDatasetDescription:
        dataset, version = _parse_snapshot_id(snapshot_id)
        return self._describe_dataset.execute(dataset, version)

    def materialize(
        self,
        snapshot_id: str,
        *,
        destination: str | Path | None = None,
    ) -> DerivedMaterializedDataset:
        dataset, version = _parse_snapshot_id(snapshot_id)
        return self._materialize_dataset.execute(
            dataset,
            version,
            destination=Path(destination) if destination is not None else self._cache_dir,
        )

    def file(
        self,
        snapshot_id: str,
        file_name: str | None = None,
        *,
        destination: str | Path | None = None,
    ) -> Path:
        dataset, version = _parse_snapshot_id(snapshot_id)
        return self._materialize_file.execute(
            dataset,
            version,
            file_name,
            destination=Path(destination) if destination is not None else self._cache_dir,
        )

    def files(
        self,
        snapshot_id: str,
        *,
        destination: str | Path | None = None,
    ) -> dict[str, Path]:
        return self.materialize(snapshot_id, destination=destination).files


class ExternalRegistryClient:
    """Register and inspect sanitized metadata-only external versions."""

    def __init__(
        self,
        register_version: RegisterExternalDatasetVersion,
        describe_version: DescribeExternalDatasetVersion,
        list_versions: ListExternalDatasetVersions,
    ):
        self._register_version = register_version
        self._describe_version = describe_version
        self._list_versions = list_versions

    def register(
        self,
        snapshot_id: str,
        *,
        interface: str,
        access_mode: str,
        service_name: str,
        observed_at: datetime,
        version_date: date | None = None,
        documentation_url: str | None = None,
        version_check: Mapping[str, Any] | None = None,
        version_evidence: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExternalDatasetDescription:
        dataset, version_value = _parse_snapshot_id(snapshot_id)
        return self._register_version.execute(
            ExternalDatasetVersion(
                dataset=dataset,
                version=DatasetVersion(version_value, version_date=version_date),
                interface=interface,
                access_mode=access_mode,
                service_name=service_name,
                observed_at=observed_at,
                documentation_url=documentation_url,
                version_check=version_check or {},
                version_evidence=version_evidence or {},
                metadata=metadata or {},
            )
        )

    def describe(self, snapshot_id: str) -> ExternalDatasetDescription:
        dataset, version = _parse_snapshot_id(snapshot_id)
        return self._describe_version.execute(dataset, version)

    def list(
        self,
        *,
        source: str | None = None,
        dataset: str | None = None,
    ) -> tuple[ExternalDatasetDescription, ...]:
        return self._list_versions.execute(source=source, dataset=dataset)


def _json_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    try:
        encoded = json.dumps(dict(value), sort_keys=True, allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise InvalidPublicationError(f"{label} must be JSON-safe: {error}") from error
    if not isinstance(decoded, dict):
        raise InvalidPublicationError(f"{label} must be a mapping")
    return decoded
