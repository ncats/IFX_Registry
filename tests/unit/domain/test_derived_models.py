"""Domain invariants for caller-produced derived datasets."""

from pathlib import PurePosixPath

import pytest

from ifx_registry import ProducerIdentity, SnapshotRef
from ifx_registry.domain.errors import InvalidPublicationError
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    DerivedSnapshot,
    DerivedSnapshotFile,
)


def _producer() -> ProducerIdentity:
    return ProducerIdentity(
        name="ifx_harmonizers",
        release="2.2.0",
        code_repository="https://github.com/ncats/IFX_harmonizers",
        code_revision="a" * 40,
    )


def test_snapshot_ref_preserves_kind_separately_from_three_part_id() -> None:
    source = SnapshotRef.source("uniprot:human:2026_03")
    derived = SnapshotRef.derived("ifx_harmonizers:targets:2.2.0")

    assert source.snapshot_id == "uniprot:human:2026_03"
    assert source.kind.value == "source_snapshot"
    assert derived.snapshot_id == "ifx_harmonizers:targets:2.2.0"
    assert derived.kind.value == "derived_snapshot"


def test_producer_requires_immutable_code_revision() -> None:
    with pytest.raises(InvalidPublicationError, match="commit hash"):
        ProducerIdentity(
            name="ifx_harmonizers",
            release="2.2.0",
            code_repository="https://github.com/ncats/IFX_harmonizers",
            code_revision="main",
        )

    with pytest.raises(InvalidPublicationError, match="commit hash"):
        ProducerIdentity(
            name="ifx_harmonizers",
            release="2.2.0",
            code_repository="https://github.com/ncats/IFX_harmonizers",
            code_revision="abcdef0",
        )


def test_derived_file_rejects_registry_manifest_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    output = tmp_path / "output.yaml"
    output.write_text("result: true\n", encoding="utf-8")

    with pytest.raises(InvalidPublicationError, match="reserved"):
        DerivedSnapshotFile(output, PurePosixPath("manifest.yaml"))


def test_derived_snapshot_requires_json_safe_provenance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    output = tmp_path / "records.tsv"
    output.write_text("id\n1\n", encoding="utf-8")

    with pytest.raises(InvalidPublicationError, match="transform must be JSON-safe"):
        DerivedSnapshot(
            dataset=DatasetId("example", "records"),
            version=DatasetVersion("1"),
            files=(DerivedSnapshotFile(output, PurePosixPath("records.tsv")),),
            inputs=(SnapshotRef.source("source:records:1"),),
            producer=_producer(),
            transform={"name": "example", "version": 1, "bad": object()},
            validation={"rows": 1},
        )


def test_derived_snapshot_canonicalizes_duplicate_inputs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    output = tmp_path / "records.tsv"
    output.write_text("id\n1\n", encoding="utf-8")
    dependency = SnapshotRef.source("source:records:1")

    snapshot = DerivedSnapshot(
        dataset=DatasetId("example", "records"),
        version=DatasetVersion("1"),
        files=(DerivedSnapshotFile(output, PurePosixPath("records.tsv")),),
        inputs=(dependency, dependency),
        producer=_producer(),
        transform={"name": "example", "version": 1},
        validation={"rows": 1},
    )

    assert snapshot.inputs == (dependency,)


def test_derived_snapshot_rejects_malformed_reserved_service_observations(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    output = tmp_path / "records.tsv"
    output.write_text("id\n1\n", encoding="utf-8")

    with pytest.raises(InvalidPublicationError, match="service_observations must be a list"):
        DerivedSnapshot(
            dataset=DatasetId("example", "records"),
            version=DatasetVersion("1"),
            files=(DerivedSnapshotFile(output, PurePosixPath("records.tsv")),),
            inputs=(SnapshotRef.source("source:records:1"),),
            producer=_producer(),
            transform={"name": "example", "version": 1},
            validation={"rows": 1},
            metadata={"service_observations": "not-a-list"},
        )


@pytest.mark.parametrize(
    "unsafe_observation, expected_message",
    [
        ({"api_key": "do-not-publish"}, "unsupported fields"),
        (
            {
                "endpoint_template": (
                    "https://example.org/records/{record_id}?token=do-not-publish"
                )
            },
            "credentials",
        ),
        (
            {"endpoint_template": "https://example.org/records?format=json"},
            "without a query or fragment",
        ),
        ({"request_count": "1"}, "request_count must be a nonnegative integer"),
        ({"service_name": 123}, "service_name must be a string"),
    ],
)
def test_derived_snapshot_rejects_credentials_in_service_observations(
    tmp_path,
    unsafe_observation,
    expected_message,
) -> None:  # type: ignore[no-untyped-def]
    output = tmp_path / "records.tsv"
    output.write_text("id\n1\n", encoding="utf-8")
    observation = {
        "service_id": "example:records_api",
        "service_name": "Example API",
        "interface": "rest",
        "operation": "retrieve records",
        "endpoint_template": "https://example.org/records/{record_id}",
        "first_observed_at": "2026-09-11T12:00:00+00:00",
        "last_observed_at": "2026-09-11T12:01:00+00:00",
        "request_count": 1,
        "retry_count": 0,
        "http_status_counts": {"200": 1},
        "worst_throttle": "green",
        "response_payload_sha256": "a" * 64,
        **unsafe_observation,
    }

    with pytest.raises(InvalidPublicationError, match=expected_message):
        DerivedSnapshot(
            dataset=DatasetId("example", "records"),
            version=DatasetVersion("1"),
            files=(DerivedSnapshotFile(output, PurePosixPath("records.tsv")),),
            inputs=(SnapshotRef.source("source:records:1"),),
            producer=_producer(),
            transform={"name": "example", "version": 1},
            validation={"rows": 1},
            metadata={"service_observations": [observation]},
        )
