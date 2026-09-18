from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from ifx_registry.client import _parse_snapshot_id
from ifx_registry.domain.errors import (
    InvalidSnapshotIdError,
    RegistryClientConfigurationError,
)
from ifx_registry.domain.models import DatasetId


def test_parse_pinned_snapshot_id() -> None:
    assert _parse_snapshot_id("reactome:pathways:97") == (
        DatasetId("reactome", "pathways"),
        "97",
    )


def test_catalog_types_are_available_from_public_package() -> None:
    from ifx_registry import PublishedFile, PublishedSnapshot

    assert PublishedFile.__name__ == "PublishedFile"
    assert PublishedSnapshot.__name__ == "PublishedSnapshot"


def test_audit_client_exposes_advanced_catalog_when_needed() -> None:
    from ifx_registry import RegistryAuditClient

    expected = (object(),)

    class BrowseSpy:
        def execute(self):  # type: ignore[no-untyped-def]
            return expected

    client = RegistryAuditClient(
        cast(Any, BrowseSpy()),
        cast(Any, object()),
        cast(Any, object()),
    )

    assert cast(Any, client.catalog()) == expected


def test_audit_client_source_and_derived_conveniences_use_kind_qualified_roots() -> None:
    from ifx_registry import (
        AuditDisposition,
        ReferenceAudit,
        RegistryAudit,
        RegistryAuditClient,
        SnapshotRef,
    )

    class AuditSpy:
        calls = []

        def execute(self, roots, *, timeout):  # type: ignore[no-untyped-def]
            self.calls.append((tuple(roots), timeout))
            entries = tuple(
                ReferenceAudit(root, AuditDisposition.CURRENT, pin_registered=True)
                for root in roots
            )
            return RegistryAudit(tuple(roots), entries, datetime.now(UTC))

    audit = AuditSpy()
    client = RegistryAuditClient(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, audit),
    )

    source = client.assess_source("example:records:1")
    derived = client.assess_derived("example:output:2")

    assert source.reference == SnapshotRef.source("example:records:1")
    assert derived.reference == SnapshotRef.derived("example:output:2")
    assert [call[0] for call in audit.calls] == [
        (source.reference,),
        (derived.reference,),
    ]


@pytest.mark.parametrize("value", ["reactome:pathways", "a:b:c:d", "a::1", ""])
def test_parse_snapshot_id_requires_exact_pin(value: str) -> None:
    with pytest.raises(InvalidSnapshotIdError):
        _parse_snapshot_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "Reactome:pathways:97",
        "reactome:Pathways:97",
        "reactome:pathways:v/1",
    ],
)
def test_parse_snapshot_id_translates_invalid_components(value: str) -> None:
    with pytest.raises(InvalidSnapshotIdError, match="source:dataset:version"):
        _parse_snapshot_id(value)


def test_public_client_accepts_path_destinations() -> None:
    class MaterializeSpy:
        def execute(self, dataset, version, *, destination):  # type: ignore[no-untyped-def]
            return dataset, version, destination

    from ifx_registry import RegistryClient

    result = RegistryClient(
        cast(Any, MaterializeSpy()),
        cast(Any, object()),
    ).materialize(
        "reactome:pathways:97",
        destination="registry-cache",
    )

    assert cast(Any, result) == (
        DatasetId("reactome", "pathways"),
        "97",
        Path("registry-cache"),
    )


def test_public_client_describes_snapshot_without_materializing() -> None:
    sentinel = object()

    class DescribeSpy:
        def execute(self, dataset, version):  # type: ignore[no-untyped-def]
            assert dataset == DatasetId("reactome", "pathways")
            assert version == "97"
            return sentinel

    from ifx_registry import RegistryClient

    result = RegistryClient(
        cast(Any, object()),
        cast(Any, object()),
        describe_dataset=cast(Any, DescribeSpy()),
    ).describe("reactome:pathways:97")

    assert result is sentinel


def test_manually_injected_client_explains_missing_describe_capability() -> None:
    from ifx_registry import RegistryClient

    client = RegistryClient(cast(Any, object()), cast(Any, object()))

    with pytest.raises(RegistryClientConfigurationError, match="use RegistryClient.connect"):
        client.describe("reactome:pathways:97")


def test_manually_injected_client_explains_missing_derived_capability() -> None:
    from ifx_registry import RegistryClient

    client = RegistryClient(cast(Any, object()), cast(Any, object()))

    with pytest.raises(RegistryClientConfigurationError, match="derived interface"):
        _ = client.derived


def test_derived_namespace_builds_explicit_caller_owned_publication(tmp_path: Path) -> None:
    from ifx_registry import DerivedRegistryClient, ProducerIdentity, SnapshotRef

    output = tmp_path / "producer-output.tsv"
    output.write_text("id\n1\n", encoding="utf-8")
    sentinel = object()

    class PublishSpy:
        snapshot = None

        def execute(self, snapshot):  # type: ignore[no-untyped-def]
            self.snapshot = snapshot
            return sentinel

    publisher = PublishSpy()
    derived = DerivedRegistryClient(
        cast(Any, publisher),
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
    )

    result = derived.publish(
        "ifx_harmonizers:targets:2.2.0",
        files={"nested/targets.tsv": output},
        inputs=[SnapshotRef.source("uniprot:human:2026_03")],
        producer=ProducerIdentity(
            "ifx_harmonizers",
            "2.2.0",
            "https://github.com/ncats/IFX_harmonizers",
            "a" * 40,
        ),
        transform={"name": "target_harmonization", "version": 2},
        validation={"rows": 1},
    )

    assert result is sentinel
    assert publisher.snapshot.snapshot_id == "ifx_harmonizers:targets:2.2.0"
    assert publisher.snapshot.files[0].relative_path.as_posix() == "nested/targets.tsv"
    assert publisher.snapshot.files[0].local_path == output
    assert output.read_text(encoding="utf-8") == "id\n1\n"


def test_public_client_builds_manual_source_publication(tmp_path: Path) -> None:
    from ifx_registry import RegistryClient

    output = tmp_path / "provider.tsv"
    output.write_text("id\n1\n", encoding="utf-8")
    sentinel = object()

    class PublishSpy:
        snapshot = None

        def execute(self, snapshot):  # type: ignore[no-untyped-def]
            self.snapshot = snapshot
            return sentinel

    publisher = PublishSpy()
    client = RegistryClient(
        cast(Any, object()),
        cast(Any, object()),
        publish_source=cast(Any, publisher),
    )

    result = client.publish_source(
        "provider:records:2026-09",
        files={"nested/records.tsv": output},
        captured_at=datetime(2026, 9, 10, tzinfo=UTC),
        capture_method="provider_export",
        version_evidence={"release": "2026-09"},
        validation={"rows": 1},
    )

    assert result is sentinel
    assert publisher.snapshot.files[0].source_url is None
    assert publisher.snapshot.metadata["capture_method"] == "provider_export"
    assert publisher.snapshot.metadata["version_method"] == "manual_provider_export"
    assert output.exists()


def test_manually_injected_client_explains_missing_external_capability() -> None:
    from ifx_registry import RegistryClient

    client = RegistryClient(cast(Any, object()), cast(Any, object()))

    with pytest.raises(RegistryClientConfigurationError, match="external interface"):
        _ = client.external
