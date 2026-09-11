"""Application orchestration for derived snapshot publication."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from ifx_registry import ProducerIdentity, SnapshotRef
from ifx_registry.application.use_cases.derived_datasets import PublishDerivedDataset
from ifx_registry.domain.catalog import (
    PublishedDerivedSnapshot,
    PublishedExternalDatasetVersion,
    PublishedFile,
    PublishedSnapshot,
)
from ifx_registry.domain.models import (
    DatasetId,
    DatasetVersion,
    DerivedSnapshot,
    DerivedSnapshotFile,
    ExternalDatasetVersion,
    SourceVersion,
)


def _file(uri: str) -> PublishedFile:
    return PublishedFile(
        PurePosixPath("records.tsv"),
        uri,
        5,
        hashlib.sha256(b"id\n1\n").hexdigest(),
    )


def test_publication_resolves_and_records_every_exact_input_kind(
    tmp_path: Path,
) -> None:
    source = PublishedSnapshot(
        dataset=DatasetId("source", "records"),
        version=SourceVersion("1"),
        files=(_file("s3://registry/sources/source/records/1/records.tsv"),),
        downloaded_at=datetime(2026, 9, 1, tzinfo=UTC),
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        manifest_uri="s3://registry/sources/source/records/1/manifest.yaml",
        manifest_sha256="a" * 64,
    )
    upstream = PublishedDerivedSnapshot(
        dataset=DatasetId("derived", "records"),
        version=DatasetVersion("1"),
        files=(_file("s3://registry/derived/derived/records/1/records.tsv"),),
        inputs=(),
        producer=None,
        transform={},
        validation={},
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        manifest_uri="s3://registry/derived/derived/records/1/manifest.yaml",
        manifest_sha256="b" * 64,
        build_key="c" * 64,
        publication_fingerprint="c" * 64,
    )
    external = PublishedExternalDatasetVersion(
        value=ExternalDatasetVersion(
            dataset=DatasetId("external", "database"),
            version=DatasetVersion("1"),
            interface="sql",
            access_mode="query",
            service_name="External database",
            observed_at=datetime(2026, 9, 1, tzinfo=UTC),
        ),
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        manifest_uri="s3://registry/external/external/database/1/manifest.yaml",
        manifest_sha256="e" * 64,
    )

    class Catalog:
        def __init__(self, item):  # type: ignore[no-untyped-def]
            self.item = item

        def get(self, dataset, version):  # type: ignore[no-untyped-def]
            assert (dataset, version) == (self.item.dataset, self.item.version.value)
            return self.item

    class Publisher:
        received = None

        def publish(self, snapshot, inputs):  # type: ignore[no-untyped-def]
            self.received = (snapshot, inputs)
            return replace(upstream, inputs=inputs)

    output = tmp_path / "output.tsv"
    output.write_text("id\n1\n", encoding="utf-8")
    snapshot = DerivedSnapshot(
        dataset=DatasetId("result", "records"),
        version=DatasetVersion("1"),
        files=(DerivedSnapshotFile(output, PurePosixPath("records.tsv")),),
        inputs=(
            SnapshotRef.source("source:records:1"),
            SnapshotRef.derived("derived:records:1"),
            SnapshotRef.external("external:database:1"),
        ),
        producer=ProducerIdentity(
            "producer",
            "1",
            "https://example.org/repository",
            "d" * 40,
        ),
        transform={"name": "combine", "version": 1},
        validation={"rows": 1},
    )
    publisher = Publisher()

    description = PublishDerivedDataset(
        cast(Any, Catalog(source)),
        cast(Any, Catalog(upstream)),
        cast(Any, Catalog(external)),
        cast(Any, publisher),
    ).execute(snapshot)

    assert description.snapshot.dataset == upstream.dataset
    assert publisher.received is not None
    registered = publisher.received[1]
    assert [item.snapshot_id for item in registered] == [
        "derived:records:1",
        "external:database:1",
        "source:records:1",
    ]
    assert [item.manifest_sha256 for item in registered] == [
        "b" * 64,
        "e" * 64,
        "a" * 64,
    ]
    assert description.inputs == tuple(registered)
    assert description.input_ids == (
        "derived:records:1",
        "external:database:1",
        "source:records:1",
    )
