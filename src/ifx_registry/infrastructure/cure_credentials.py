"""Load credentials for the CURE ID case-report API."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ifx_registry.domain.errors import RegistryUnavailableError


@dataclass(frozen=True, slots=True)
class CureCredentialSettings:
    api_key: str = field(repr=False)


def load_cure_credentials(path: Path) -> CureCredentialSettings:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("root must be a mapping")
        if payload.get("type", "cure_api_key") != "cure_api_key":
            raise ValueError("type must be cure_api_key")
        return CureCredentialSettings(api_key=_required(payload, "api_key"))
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise RegistryUnavailableError(
            f"Could not load CURE ID credentials from {path}: {error}"
        ) from error


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()
