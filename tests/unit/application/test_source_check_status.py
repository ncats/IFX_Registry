"""Tests for the one-week upstream check freshness policy."""

from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from ifx_registry.application.use_cases.browse_catalog import PublishedDataset
from ifx_registry.application.use_cases.source_check_status import (
    ListSourceCheckStatuses,
    SourceCheckState,
)
from ifx_registry.domain.catalog import PublishedFile, PublishedSnapshot
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.domain.version_checks import SourceVersionCheck, VersionCheckOutcome
from ifx_registry.infrastructure.version_check_store import SQLiteSourceVersionCheckStore


def test_successful_check_is_reused_for_one_week(tmp_path: Path) -> None:
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    dataset = DatasetId("example", "records")
    version = SourceVersion("2026-09", discovered_at=now - timedelta(days=6))
    store = SQLiteSourceVersionCheckStore(tmp_path / "registry.sqlite3")
    store.append(
        SourceVersionCheck(
            "check-1",
            dataset,
            now - timedelta(days=6),
            VersionCheckOutcome.SUCCEEDED,
            version=version,
        )
    )
    registered = PublishedDataset(
        dataset,
        (
            PublishedSnapshot(
                dataset=dataset,
                version=version,
                files=(
                    PublishedFile(
                        PurePosixPath("records.tsv"),
                        "s3://registry/records.tsv",
                        1,
                        "0" * 64,
                    ),
                ),
                downloaded_at=now,
                published_at=now,
                manifest_uri="s3://registry/manifest.json",
            ),
        ),
    )
    statuses = ListSourceCheckStatuses(store, clock=lambda: now)

    fresh = statuses.execute((dataset,), {dataset: registered})[dataset]
    stale = ListSourceCheckStatuses(
        store,
        clock=lambda: now + timedelta(days=2),
    ).execute((dataset,), {dataset: registered})[dataset]

    assert fresh.state is SourceCheckState.UP_TO_DATE
    assert fresh.fresh_until == now + timedelta(days=1)
    assert stale.state is SourceCheckState.CHECK_RECOMMENDED
