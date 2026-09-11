"""Tests for the explicit team AWS credential-file adapter."""

from pathlib import Path

import pytest

from ifx_registry.domain.errors import RegistryUnavailableError
from ifx_registry.infrastructure.aws_credentials import load_aws_credentials


def test_loads_existing_assume_role_yaml_shape(tmp_path: Path) -> None:
    path = tmp_path / "aws.yaml"
    path.write_text(
        """\
type: aws_assume_role
access_key_id: example-key
secret_access_key: example-secret
role_arn: arn:aws:iam::123456789012:role/example
bucket: example-registry
region: us-east-1
session_name: registry-test
""",
        encoding="utf-8",
    )

    credentials = load_aws_credentials(path)

    assert credentials.bucket == "example-registry"
    assert credentials.role_arn.endswith("role/example")
    assert credentials.session_name == "registry-test"
    assert "example-secret" not in repr(credentials)


def test_rejects_incomplete_credential_file(tmp_path: Path) -> None:
    path = tmp_path / "aws.yaml"
    path.write_text("type: aws_assume_role\nbucket: example\n", encoding="utf-8")

    with pytest.raises(RegistryUnavailableError, match="access_key_id is required"):
        load_aws_credentials(path)
