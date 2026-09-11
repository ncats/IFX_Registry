from datetime import UTC, datetime
from pathlib import PurePosixPath

from ifx_registry.application.use_cases.assess_source_update import (
    AssessSourceUpdate,
    SourceUpdateState,
)
from ifx_registry.application.use_cases.browse_catalog import PublishedDataset
from ifx_registry.domain.catalog import PublishedFile, PublishedSnapshot
from ifx_registry.domain.models import DatasetId, SourceVersion


def _snapshot(version: str, day: int) -> PublishedSnapshot:
    dataset = DatasetId("example", "records")
    return PublishedSnapshot(
        dataset=dataset,
        version=SourceVersion(version),
        files=(
            PublishedFile(
                PurePosixPath("records.tsv"),
                f"s3://registry/sources/example/records/{version}/records.tsv",
                1,
                "a" * 64,
            ),
        ),
        downloaded_at=datetime(2026, 9, day, tzinfo=UTC),
        published_at=datetime(2026, 9, day, tzinfo=UTC),
        manifest_uri=f"s3://registry/sources/example/records/{version}/manifest.yaml",
    )


def test_assessment_distinguishes_latest_registered_and_available_versions() -> None:
    registered = PublishedDataset(
        DatasetId("example", "records"),
        (_snapshot("2", 2), _snapshot("1", 1)),
    )
    assess = AssessSourceUpdate()

    assert assess.execute(SourceVersion("2"), registered).state is SourceUpdateState.UP_TO_DATE
    assert (
        assess.execute(SourceVersion("1"), registered).state is SourceUpdateState.ALREADY_REGISTERED
    )
    available = assess.execute(SourceVersion("3"), registered)
    assert available.state is SourceUpdateState.AVAILABLE
    assert available.can_register


def test_first_registered_version_is_available_without_string_ordering() -> None:
    assessment = AssessSourceUpdate().execute(SourceVersion("release-a"), None)

    assert assessment.state is SourceUpdateState.AVAILABLE
    assert assessment.latest_registered_version is None
