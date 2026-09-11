"""S3 catalog for immutable metadata-only external dataset versions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import yaml

from ifx_registry.application.ports.external import (
    ExternalDatasetVersionCatalog,
    ExternalDatasetVersionPublisher,
)
from ifx_registry.domain.catalog import PublishedExternalDatasetVersion
from ifx_registry.domain.errors import (
    CatalogConsistencyError,
    InvalidPublicationError,
    SnapshotAlreadyExistsError,
    SnapshotNotFoundError,
)
from ifx_registry.domain.models import DatasetId, DatasetVersion, ExternalDatasetVersion
from ifx_registry.infrastructure.object_store import ObjectStore
from ifx_registry.infrastructure.s3_dataset_files import S3ImmutableManifestStore

MANIFEST_SCHEMA_VERSION = 1
WIRE_KIND = "external_source_registration"


class S3ExternalDatasetVersionRepository(
    ExternalDatasetVersionCatalog,
    ExternalDatasetVersionPublisher,
):
    """Read sanitized legacy records and commit safe external assertions."""

    def __init__(self, objects: ObjectStore, *, prefix: str = ""):
        self._manifests = S3ImmutableManifestStore(objects, "external", prefix=prefix)

    def list_all(self) -> tuple[PublishedExternalDatasetVersion, ...]:
        values = [self._load(key) for key in self._manifests.list_keys()]
        return tuple(sorted(values, key=lambda item: item.published_at, reverse=True))

    def get(self, dataset: DatasetId, version: str) -> PublishedExternalDatasetVersion:
        normalized = DatasetVersion(version).value
        key = self._manifests.key(dataset, normalized)
        payload = self._manifests.read(key)
        if payload is None:
            raise SnapshotNotFoundError(
                f"External dataset version not found: {dataset}:{version}"
            )
        return self._parse(key, payload)

    def publish(self, value: ExternalDatasetVersion) -> PublishedExternalDatasetVersion:
        # Reconstruct at the write boundary so callers cannot bypass domain
        # validation by mutating a nested mapping after object construction.
        value = _validated_copy(value)
        key = self._manifests.key(value.dataset, value.version.value)
        existing_payload = self._manifests.read(key)
        if existing_payload is not None:
            existing = self._parse(key, existing_payload)
            if _same_assertion(existing.value, value):
                return existing
            raise SnapshotAlreadyExistsError(
                "External dataset version is immutable and has different metadata: "
                f"{value.snapshot_id}"
            )
        published_at = datetime.now(UTC)
        payload = _manifest_payload(value, published_at, self._manifests.uri(key))
        manifest_bytes = yaml.safe_dump(payload, sort_keys=False).encode("utf-8")
        if not self._manifests.commit(key, manifest_bytes):
            winner_payload = self._manifests.read(key)
            if winner_payload is None:
                raise CatalogConsistencyError(
                    f"External manifest commit was rejected but no manifest exists: {key}"
                )
            winner = self._parse(key, winner_payload)
            if _same_assertion(winner.value, value):
                return winner
            raise SnapshotAlreadyExistsError(
                "External dataset version was committed concurrently with different metadata: "
                f"{value.snapshot_id}"
            )
        return self._load(key)

    def _load(self, key: str) -> PublishedExternalDatasetVersion:
        payload = self._manifests.read(key)
        if payload is None:
            raise SnapshotNotFoundError(f"External manifest not found: {key}")
        return self._parse(key, payload)

    def _parse(self, key: str, content: bytes) -> PublishedExternalDatasetVersion:
        try:
            payload = yaml.safe_load(content)
            if not isinstance(payload, dict):
                raise ValueError("manifest root must be a mapping")
            if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
                raise ValueError("unsupported schema_version")
            if payload.get("kind") not in {WIRE_KIND, "external_dataset_version"}:
                raise ValueError("manifest kind is not an external dataset version")
            dataset = DatasetId(str(payload["source"]), str(payload["dataset"]))
            version = DatasetVersion(
                str(payload["version"]),
                version_date=_optional_date(payload.get("version_date")),
            )
            if key != self._manifests.key(dataset, version.value):
                raise ValueError("manifest identity does not match its S3 key")
            snapshot_id = f"{dataset}:{version}"
            registration_id = payload.get("registration_id", snapshot_id)
            if registration_id != snapshot_id:
                raise ValueError("registration_id does not match manifest identity")
            published_at = _datetime(
                payload.get("created_at") or payload.get("registered_at")
                or payload.get("registered_date")
            )
            connection = _mapping(payload.get("connection"))
            access = _mapping(payload.get("access"))
            interface = str(
                payload.get("interface")
                or connection.get("type")
                or access.get("interface")
                or access.get("database_type")
                or "external"
            )
            access_mode = str(payload.get("access_mode") or access.get("mode") or "query")
            service_name = str(
                payload.get("service_name") or f"{dataset.source} {dataset.dataset}"
            )
            documentation_url = _safe_legacy_public_url(
                payload.get("documentation_url")
                or payload.get("public_url")
                or connection.get("endpoint")
            )
            version_check, evidence = _legacy_provenance(payload)
            value = ExternalDatasetVersion(
                dataset=dataset,
                version=version,
                interface=interface,
                access_mode=access_mode,
                service_name=service_name,
                observed_at=_datetime(
                    payload.get("observed_at") or payload.get("version_date") or published_at
                ),
                documentation_url=documentation_url,
                version_check=version_check,
                version_evidence=evidence,
                metadata=_safe_mapping(payload.get("metadata")),
            )
            return PublishedExternalDatasetVersion(
                value=value,
                published_at=published_at,
                manifest_uri=self._manifests.uri(key),
                manifest_sha256=hashlib.sha256(content).hexdigest(),
            )
        except (KeyError, TypeError, ValueError, InvalidPublicationError, yaml.YAMLError) as error:
            raise CatalogConsistencyError(
                f"Invalid external Registry manifest {key}: {error}"
            ) from error


def _manifest_payload(
    value: ExternalDatasetVersion,
    published_at: datetime,
    manifest_uri: str,
) -> dict[str, Any]:
    return {
        "kind": WIRE_KIND,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source": value.dataset.source,
        "dataset": value.dataset.dataset,
        "registration_id": value.snapshot_id,
        "version": value.version.value,
        "version_date": value.version.version_date,
        "observed_at": value.observed_at,
        "created_at": published_at,
        "interface": value.interface,
        "access_mode": value.access_mode,
        "service_name": value.service_name,
        "documentation_url": value.documentation_url,
        "version_check": dict(value.version_check),
        "version_evidence": dict(value.version_evidence),
        "metadata": dict(value.metadata),
        "manifest_uri": manifest_uri,
    }


def _validated_copy(value: ExternalDatasetVersion) -> ExternalDatasetVersion:
    return ExternalDatasetVersion(
        dataset=value.dataset,
        version=value.version,
        interface=value.interface,
        access_mode=value.access_mode,
        service_name=value.service_name,
        observed_at=value.observed_at,
        documentation_url=value.documentation_url,
        version_check=value.version_check,
        version_evidence=value.version_evidence,
        metadata=value.metadata,
    )


def _same_assertion(left: ExternalDatasetVersion, right: ExternalDatasetVersion) -> bool:
    return _canonical_assertion(left) == _canonical_assertion(right)


def _canonical_assertion(value: ExternalDatasetVersion) -> str:
    return json.dumps(
        {
            "dataset": str(value.dataset),
            "version": value.version.value,
            "version_date": value.version.version_date.isoformat()
            if value.version.version_date
            else None,
            "observed_at": value.observed_at.isoformat(),
            "interface": value.interface,
            "access_mode": value.access_mode,
            "service_name": value.service_name,
            "documentation_url": value.documentation_url,
            "version_check": value.version_check,
            "version_evidence": value.version_evidence,
            "metadata": value.metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _legacy_provenance(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    direct_check = _safe_mapping(payload.get("version_check"))
    direct_evidence = _safe_mapping(payload.get("version_evidence"))
    if direct_check or direct_evidence:
        return direct_check, direct_evidence
    extra = _mapping(payload.get("extra"))
    method = _mapping(extra.get("version_method"))
    version_check = {
        key: method[key]
        for key in ("type", "description")
        if key in method and isinstance(method[key], (str, int, float, bool))
    }
    return _safe_mapping(version_check), _safe_mapping(method.get("evidence"))


def _safe_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    try:
        probe = ExternalDatasetVersion(
            DatasetId("sanitizer", "probe"),
            DatasetVersion("1"),
            "external",
            "query",
            "Sanitizer",
            datetime.now(UTC),
            metadata=value,
        )
    except InvalidPublicationError:
        return {}
    return dict(probe.metadata)


def _safe_legacy_public_url(value: object) -> str | None:
    if value is None:
        return None
    try:
        probe = ExternalDatasetVersion(
            DatasetId("sanitizer", "probe"),
            DatasetVersion("1"),
            "external",
            "query",
            "Sanitizer",
            datetime.now(UTC),
            documentation_url=str(value),
        )
    except InvalidPublicationError:
        return None
    return probe.documentation_url


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("manifest timestamp is missing")
    return result if result.tzinfo is not None else result.replace(tzinfo=UTC)


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
