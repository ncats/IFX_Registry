"""Tests for loading CURE ID API credentials."""

from pathlib import Path

import pytest

from ifx_registry.domain.errors import RegistryUnavailableError
from ifx_registry.infrastructure.cure_credentials import load_cure_credentials


def test_loads_cure_api_key_without_exposing_it_in_repr(tmp_path: Path) -> None:
    path = tmp_path / "cure.yaml"
    path.write_text("type: cure_api_key\napi_key: test-secret\n", encoding="utf-8")

    credentials = load_cure_credentials(path)

    assert credentials.api_key == "test-secret"
    assert "test-secret" not in repr(credentials)


def test_rejects_missing_cure_api_key(tmp_path: Path) -> None:
    path = tmp_path / "cure.yaml"
    path.write_text("type: cure_api_key\n", encoding="utf-8")

    with pytest.raises(RegistryUnavailableError, match="api_key"):
        load_cure_credentials(path)
