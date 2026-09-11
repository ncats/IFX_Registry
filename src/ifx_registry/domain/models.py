"""Framework-independent Registry domain objects."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from ifx_registry.domain.errors import InvalidDatasetIdError, InvalidPublicationError

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IMMUTABLE_CODE_REVISION = re.compile(
    r"^(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})$"
)
_RESERVED_SNAPSHOT_PATHS = {PurePosixPath("manifest.yaml")}


def _validate_registry_name(value: str, label: str) -> str:
    normalized = value.strip()
    if not _SAFE_NAME.fullmatch(normalized):
        raise InvalidDatasetIdError(
            f"{label} must start with a lowercase letter or number and contain only "
            "lowercase letters, numbers, dots, underscores, or hyphens"
        )
    return normalized


def _validate_version(value: str, label: str = "version") -> str:
    normalized = value.strip()
    if not _SAFE_VERSION.fullmatch(normalized):
        raise ValueError(
            f"{label} must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class DatasetId:
    """Stable identity of one logical dataset from one source."""

    source: str
    dataset: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _validate_registry_name(self.source, "source"))
        object.__setattr__(self, "dataset", _validate_registry_name(self.dataset, "dataset"))

    def __str__(self) -> str:
        return f"{self.source}:{self.dataset}"


@dataclass(frozen=True, slots=True)
class SourceVersion:
    """An upstream version and the evidence used to identify it."""

    value: str
    version_date: date | None = None
    discovered_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = _validate_version(self.value, "source version")
        if self.discovered_at.tzinfo is None:
            raise ValueError("discovered_at must be timezone-aware")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    """An exact version of source or derived data."""

    value: str
    version_date: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_version(self.value))

    def __str__(self) -> str:
        return self.value


class SnapshotKind(StrEnum):
    SOURCE = "source_snapshot"
    DERIVED = "derived_snapshot"
    EXTERNAL = "external_dataset_version"


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    """An exact, kind-qualified dependency on a registered dataset snapshot."""

    kind: SnapshotKind
    dataset: DatasetId
    version: DatasetVersion

    @classmethod
    def source(cls, snapshot_id: str) -> SnapshotRef:
        return cls._parse(SnapshotKind.SOURCE, snapshot_id)

    @classmethod
    def derived(cls, snapshot_id: str) -> SnapshotRef:
        return cls._parse(SnapshotKind.DERIVED, snapshot_id)

    @classmethod
    def external(cls, snapshot_id: str) -> SnapshotRef:
        return cls._parse(SnapshotKind.EXTERNAL, snapshot_id)

    @classmethod
    def _parse(cls, kind: SnapshotKind, snapshot_id: str) -> SnapshotRef:
        parts = snapshot_id.split(":")
        if len(parts) != 3 or any(not part for part in parts):
            raise InvalidPublicationError(
                f"Pinned input must be source:dataset:version, got {snapshot_id!r}"
            )
        try:
            return cls(kind, DatasetId(parts[0], parts[1]), DatasetVersion(parts[2]))
        except ValueError as error:
            raise InvalidPublicationError(
                f"Pinned input must be source:dataset:version, got {snapshot_id!r}"
            ) from error

    @property
    def snapshot_id(self) -> str:
        return f"{self.dataset}:{self.version}"


_FORBIDDEN_EXTERNAL_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "accesskey",
    "access_key",
    "authorization",
    "username",
    "user_name",
    "credential",
    "privatekey",
    "private_key",
    "rolearn",
    "role_arn",
    "internalurl",
    "internal_url",
    "connection",
)

_CREDENTIAL_VALUE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"authorization|username|credential|role[_-]?arn)\s*[:=]"
)
_INTERNAL_HOST_VALUE = re.compile(
    r"(?i)(?<![a-z0-9.-])(localhost|[a-z0-9.-]+\.(?:internal|local|localhost))"
    r"(?::\d+)?(?![a-z0-9.-])"
)
_IP_VALUE = re.compile(r"(?<![0-9a-f:.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.])")


@dataclass(frozen=True, slots=True)
class ExternalDatasetVersion:
    """A sanitized assertion about data accessed outside Registry storage."""

    dataset: DatasetId
    version: DatasetVersion
    interface: str
    access_mode: str
    service_name: str
    observed_at: datetime
    documentation_url: str | None = None
    version_check: Mapping[str, Any] = field(default_factory=dict)
    version_evidence: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise InvalidPublicationError("external observed_at must be timezone-aware")
        interface = _validate_external_label(self.interface, "interface")
        access_mode = _validate_external_label(self.access_mode, "access_mode")
        service_name = self.service_name.strip()
        if not service_name:
            raise InvalidPublicationError("external service_name must not be blank")
        _reject_external_text(service_name, "service_name")
        documentation_url = (
            _validate_public_url(self.documentation_url, "documentation_url")
            if self.documentation_url is not None
            else None
        )
        object.__setattr__(self, "interface", interface)
        object.__setattr__(self, "access_mode", access_mode)
        object.__setattr__(self, "service_name", service_name)
        object.__setattr__(self, "documentation_url", documentation_url)
        object.__setattr__(
            self, "version_check", _external_mapping(self.version_check, "version_check")
        )
        object.__setattr__(
            self,
            "version_evidence",
            _external_mapping(self.version_evidence, "version_evidence"),
        )
        object.__setattr__(self, "metadata", _external_mapping(self.metadata, "metadata"))

    @property
    def snapshot_id(self) -> str:
        return f"{self.dataset}:{self.version}"


def _validate_external_label(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not _SAFE_NAME.fullmatch(normalized):
        raise InvalidPublicationError(
            f"external {label} must contain lowercase letters, numbers, dots, underscores, "
            "or hyphens"
        )
    return normalized


def _validate_public_url(value: str, label: str) -> str:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InvalidPublicationError(f"external {label} must be a public HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidPublicationError(f"external {label} must not contain credentials")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith((".local", ".internal", ".localhost")):
        raise InvalidPublicationError(f"external {label} must not be an internal URL")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise InvalidPublicationError(f"external {label} must not be an internal URL")
    return normalized


def _validate_public_endpoint_template(value: str, label: str) -> str:
    normalized = _validate_public_url(value, label)
    parsed = urlsplit(normalized)
    if parsed.query or parsed.fragment:
        raise InvalidPublicationError(
            f"external {label} must be an endpoint template without a query or fragment"
        )
    return normalized


def _external_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    result = _json_mapping(value, label)
    _reject_external_secrets(result, label)
    return result


def _reject_external_secrets(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            compact = normalized.replace("_", "")
            if any(
                token in normalized or token.replace("_", "") in compact
                for token in _FORBIDDEN_EXTERNAL_KEYS
            ):
                raise InvalidPublicationError(
                    f"external metadata field {path}.{key} may contain credentials or "
                    "internal connection details"
                )
            _reject_external_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_external_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        _reject_external_text(value, path)
    elif isinstance(value, str):
        _reject_external_text(value, path)


def _reject_external_text(value: str, path: str) -> None:
    normalized = value.strip()
    lowered = normalized.lower()
    if lowered.startswith(("http://", "https://")):
        _validate_public_url(normalized, path)
    elif "://" in normalized or lowered.startswith("arn:"):
        raise InvalidPublicationError(
            f"external metadata field {path} must not contain a connection URI or ARN"
        )
    if _CREDENTIAL_VALUE.search(normalized) or re.search(
        r"\b[^\s:/@]+:[^\s/@]+@[a-z0-9.-]+", normalized, re.IGNORECASE
    ):
        raise InvalidPublicationError(
            f"external metadata field {path} may contain credentials"
        )
    if _INTERNAL_HOST_VALUE.search(normalized):
        raise InvalidPublicationError(
            f"external metadata field {path} must not contain an internal location"
        )
    for candidate in _IP_VALUE.findall(normalized):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if not address.is_global:
            raise InvalidPublicationError(
                f"external metadata field {path} must not contain an internal location"
            )


@dataclass(frozen=True, slots=True)
class ProducerIdentity:
    """Immutable identity of the code release that produced derived data."""

    name: str
    release: str
    code_repository: str
    code_revision: str

    def __post_init__(self) -> None:
        try:
            name = _validate_registry_name(self.name, "producer name")
            release = _validate_version(self.release, "producer release")
        except ValueError as error:
            raise InvalidPublicationError(str(error)) from error
        repository = self.code_repository.strip()
        if not repository:
            raise InvalidPublicationError("producer code_repository must not be blank")
        revision = self.code_revision.strip().lower()
        if not _IMMUTABLE_CODE_REVISION.fullmatch(revision):
            raise InvalidPublicationError(
                "producer code_revision must be a Git commit hash or sha256 digest"
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "release", release)
        object.__setattr__(self, "code_repository", repository)
        object.__setattr__(self, "code_revision", revision)


@dataclass(frozen=True, slots=True)
class DerivedSnapshotFile:
    """One caller-owned file to register in a derived dataset snapshot."""

    local_path: Path
    relative_path: PurePosixPath
    content_type: str | None = None

    def __post_init__(self) -> None:
        local_path = Path(self.local_path)
        relative_path = PurePosixPath(self.relative_path)
        if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
            raise InvalidPublicationError(
                "derived file name must be a safe relative POSIX path"
            )
        if relative_path in _RESERVED_SNAPSHOT_PATHS:
            raise InvalidPublicationError(
                f"derived file name is reserved by the Registry: {relative_path}"
            )
        object.__setattr__(self, "local_path", local_path)
        object.__setattr__(self, "relative_path", relative_path)


@dataclass(frozen=True, slots=True)
class DerivedSnapshot:
    """Caller-produced data and provenance ready for immutable registration."""

    dataset: DatasetId
    version: DatasetVersion
    files: tuple[DerivedSnapshotFile, ...]
    inputs: tuple[SnapshotRef, ...]
    producer: ProducerIdentity
    transform: Mapping[str, Any]
    validation: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.files:
            raise InvalidPublicationError("derived snapshot must contain at least one file")
        paths = [file.relative_path for file in self.files]
        if len(paths) != len(set(paths)):
            raise InvalidPublicationError("derived snapshot file paths must be unique")
        if not self.inputs:
            raise InvalidPublicationError("derived snapshot must declare at least one exact input")
        inputs = tuple(
            sorted(set(self.inputs), key=lambda item: (item.kind.value, item.snapshot_id))
        )
        if any(
            item.snapshot_id == self.snapshot_id and item.kind is SnapshotKind.DERIVED
            for item in inputs
        ):
            raise InvalidPublicationError("derived snapshot cannot depend on itself")
        transform = _json_mapping(self.transform, "transform")
        if not str(transform.get("name", "")).strip() or not str(
            transform.get("version", "")
        ).strip():
            raise InvalidPublicationError("transform must declare non-blank name and version")
        validation = _json_mapping(self.validation, "validation")
        if not validation:
            raise InvalidPublicationError("validation summary must not be empty")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "transform", transform)
        object.__setattr__(self, "validation", validation)
        metadata = _json_mapping(self.metadata, "metadata")
        if "service_observations" in metadata:
            validate_service_observations(metadata["service_observations"])
        object.__setattr__(self, "metadata", metadata)

    @property
    def snapshot_id(self) -> str:
        return f"{self.dataset}:{self.version}"


def _json_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    try:
        encoded = json.dumps(dict(value), sort_keys=True, allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise InvalidPublicationError(f"{label} must be JSON-safe: {error}") from error
    if not isinstance(decoded, dict):
        raise InvalidPublicationError(f"{label} must be a mapping")
    return decoded


_SERVICE_OBSERVATION_FIELDS = {
    "service_id",
    "service_name",
    "interface",
    "operation",
    "endpoint_template",
    "first_observed_at",
    "last_observed_at",
    "request_count",
    "retry_count",
    "http_status_counts",
    "worst_throttle",
    "response_payload_sha256",
}


def validate_service_observations(value: object) -> None:
    """Validate the reserved derived-metadata representation for live services."""

    if not isinstance(value, list):
        raise InvalidPublicationError("service_observations must be a list")
    for index, observation in enumerate(value):
        label = f"service_observations[{index}]"
        if not isinstance(observation, dict):
            raise InvalidPublicationError(f"{label} must be a mapping")
        missing = _SERVICE_OBSERVATION_FIELDS - set(observation)
        if missing:
            raise InvalidPublicationError(
                f"{label} is missing fields: {', '.join(sorted(missing))}"
            )
        unexpected = set(observation) - _SERVICE_OBSERVATION_FIELDS
        if unexpected:
            raise InvalidPublicationError(
                f"{label} has unsupported fields: {', '.join(sorted(unexpected))}"
            )
        _reject_external_secrets(observation, label)
        for field_name in (
            "service_id",
            "service_name",
            "interface",
            "operation",
            "endpoint_template",
            "first_observed_at",
            "last_observed_at",
            "worst_throttle",
            "response_payload_sha256",
        ):
            if not isinstance(observation[field_name], str):
                raise InvalidPublicationError(f"{label}.{field_name} must be a string")
        service_parts = observation["service_id"].split(":")
        if len(service_parts) != 2:
            raise InvalidPublicationError(f"{label}.service_id must be source:service")
        DatasetId(service_parts[0], service_parts[1])
        for field_name in ("service_name", "operation"):
            text = observation[field_name].strip()
            if not text:
                raise InvalidPublicationError(f"{label}.{field_name} must not be blank")
            _reject_external_text(text, f"{label}.{field_name}")
        _validate_external_label(observation["interface"], f"{label}.interface")
        _validate_public_endpoint_template(
            observation["endpoint_template"],
            f"{label}.endpoint_template",
        )
        first = _observation_datetime(observation["first_observed_at"], label)
        last = _observation_datetime(observation["last_observed_at"], label)
        if last < first:
            raise InvalidPublicationError(f"{label} observation window is reversed")
        request_count = _nonnegative_observation_int(
            observation["request_count"], f"{label}.request_count"
        )
        retry_count = _nonnegative_observation_int(
            observation["retry_count"], f"{label}.retry_count"
        )
        if retry_count > request_count:
            raise InvalidPublicationError(f"{label}.retry_count exceeds request_count")
        statuses = observation["http_status_counts"]
        if not isinstance(statuses, dict) or any(
            not isinstance(status, str)
            or not status.strip()
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for status, count in statuses.items()
        ):
            raise InvalidPublicationError(
                f"{label}.http_status_counts must map statuses to nonnegative integers"
            )
        if sum(statuses.values()) != request_count:
            raise InvalidPublicationError(
                f"{label}.http_status_counts must total request_count"
            )
        if observation["worst_throttle"] not in {
            "unknown",
            "green",
            "yellow",
            "red",
            "black",
        }:
            raise InvalidPublicationError(f"{label}.worst_throttle is unsupported")
        if not re.fullmatch(r"[0-9a-f]{64}", observation["response_payload_sha256"]):
            raise InvalidPublicationError(
                f"{label}.response_payload_sha256 must be a SHA-256 digest"
            )


def _observation_datetime(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError as error:
        raise InvalidPublicationError(f"{label} has an invalid observation timestamp") from error
    if parsed is None:
        raise InvalidPublicationError(f"{label} has an invalid observation timestamp")
    if parsed.tzinfo is None:
        raise InvalidPublicationError(f"{label} observation timestamps need a timezone")
    return parsed


def _nonnegative_observation_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidPublicationError(f"{label} must be a nonnegative integer")
    return value


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    """One downloaded file and its stable name within a source snapshot."""

    local_path: Path
    relative_path: PurePosixPath
    source_url: str | None
    content_type: str | None = None

    def __post_init__(self) -> None:
        relative_path = PurePosixPath(self.relative_path)
        if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
            raise ValueError("snapshot file path must be a safe relative POSIX path")
        if relative_path in _RESERVED_SNAPSHOT_PATHS:
            raise ValueError(f"snapshot file path is reserved by the Registry: {relative_path}")
        if self.source_url is not None and not self.source_url.strip():
            raise ValueError("snapshot file source_url must not be blank")
        object.__setattr__(self, "local_path", Path(self.local_path))
        object.__setattr__(self, "relative_path", relative_path)
        object.__setattr__(
            self,
            "source_url",
            self.source_url.strip() if self.source_url is not None else None,
        )


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Files fetched for one exact version of a source dataset."""

    dataset: DatasetId
    version: SourceVersion
    files: tuple[SnapshotFile, ...]
    downloaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    homepage: str | None = None
    upstream_urls: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.files:
            raise ValueError("source snapshot must contain at least one file")
        if self.downloaded_at.tzinfo is None:
            raise ValueError("downloaded_at must be timezone-aware")
        paths = [file.relative_path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("source snapshot file paths must be unique")

    @property
    def snapshot_id(self) -> str:
        return f"{self.dataset}:{self.version}"
