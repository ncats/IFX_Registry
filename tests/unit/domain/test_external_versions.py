"""Domain invariants for metadata-only external dataset versions."""

from datetime import UTC, datetime

import pytest

from ifx_registry import ExternalDatasetVersion, SnapshotRef
from ifx_registry.domain.errors import InvalidPublicationError
from ifx_registry.domain.models import DatasetId, DatasetVersion


def _value(**changes):  # type: ignore[no-untyped-def]
    values = {
        "dataset": DatasetId("chembl", "activity_database"),
        "version": DatasetVersion("chembl36"),
        "interface": "mysql",
        "access_mode": "query",
        "service_name": "ChEMBL activity database",
        "observed_at": datetime(2026, 9, 10, tzinfo=UTC),
        "documentation_url": "https://www.ebi.ac.uk/chembl/",
        "version_evidence": {"schema_release": "chembl_36"},
    }
    values.update(changes)
    return ExternalDatasetVersion(**values)


def test_external_reference_preserves_kind() -> None:
    reference = SnapshotRef.external("chembl:activity_database:chembl36")

    assert reference.kind.value == "external_dataset_version"
    assert reference.snapshot_id == "chembl:activity_database:chembl36"


@pytest.mark.parametrize(
    "field",
    ["password", "api_token", "access_key_id", "username", "credential_ref", "role_arn"],
)
def test_external_metadata_rejects_credential_fields(field: str) -> None:
    with pytest.raises(InvalidPublicationError, match="credentials"):
        _value(metadata={"nested": {field: "must-not-be-stored"}})


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/data",
        "https://user:password@example.org/data",
        "http://127.0.0.1/data",
        "https://database.internal/data",
    ],
)
def test_external_documentation_must_be_public_and_credential_free(url: str) -> None:
    with pytest.raises(InvalidPublicationError):
        _value(documentation_url=url)


def test_external_value_normalizes_interface_and_access_mode() -> None:
    value = _value(interface=" MySQL ", access_mode=" QUERY ")

    assert value.interface == "mysql"
    assert value.access_mode == "query"


@pytest.mark.parametrize(
    ("changes"),
    [
        {"service_name": "postgresql://user:password@database.internal/chembl"},
        {"metadata": {"note": "postgresql://user:password@database.internal/chembl"}},
        {"version_evidence": {"note": "read from database.internal"}},
        {"version_check": {"note": "password=hunter2"}},
        {"metadata": {"note": "read from 10.0.0.1"}},
    ],
)
def test_external_text_rejects_connections_credentials_and_internal_locations(
    changes: dict[str, object],
) -> None:
    with pytest.raises(InvalidPublicationError):
        _value(**changes)


def test_external_metadata_allows_valid_public_documentation() -> None:
    value = _value(metadata={"documentation": "https://www.ebi.ac.uk/chembl/"})

    assert value.metadata["documentation"] == "https://www.ebi.ac.uk/chembl/"
