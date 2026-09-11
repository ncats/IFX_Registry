"""Load the team's AWS assume-role credential file."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ifx_registry.domain.errors import RegistryUnavailableError


@dataclass(frozen=True, slots=True)
class AwsCredentialSettings:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    role_arn: str
    bucket: str
    region: str = "us-east-1"
    session_name: str = "ifx-registry"
    external_id: str | None = field(default=None, repr=False)
    endpoint_url: str | None = None


def load_aws_credentials(path: Path) -> AwsCredentialSettings:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("root must be a mapping")
        if payload.get("type", "aws_assume_role") != "aws_assume_role":
            raise ValueError("type must be aws_assume_role")
        return AwsCredentialSettings(
            access_key_id=_required(payload, "access_key_id"),
            secret_access_key=_required(payload, "secret_access_key"),
            role_arn=_required(payload, "role_arn"),
            bucket=_required(payload, "bucket"),
            region=_optional(payload, "region") or "us-east-1",
            session_name=_optional(payload, "session_name") or "ifx-registry",
            external_id=_optional(payload, "external_id"),
            endpoint_url=_optional(payload, "endpoint_url"),
        )
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise RegistryUnavailableError(
            f"Could not load Registry AWS credentials from {path}: {error}"
        ) from error


def _required(payload: dict[str, Any], key: str) -> str:
    value = _optional(payload, key)
    if value is None:
        raise ValueError(f"{key} is required")
    return value


def _optional(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()
