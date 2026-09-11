"""Tests for persisted upstream version observations."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ifx_registry.domain.errors import OperationalStateUnavailableError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.domain.version_checks import SourceVersionCheck, VersionCheckOutcome
from ifx_registry.infrastructure.version_check_store import SQLiteSourceVersionCheckStore


def test_version_check_history_preserves_latest_attempt_and_success(tmp_path: Path) -> None:
    store = SQLiteSourceVersionCheckStore(tmp_path / "registry.sqlite3")
    dataset = DatasetId("example", "records")
    checked_at = datetime(2026, 9, 9, 12, tzinfo=UTC)
    success = SourceVersionCheck(
        "check-1",
        dataset,
        checked_at,
        VersionCheckOutcome.SUCCEEDED,
        version=SourceVersion(
            "2026-09",
            version_date=date(2026, 9, 1),
            discovered_at=checked_at,
            evidence={"endpoint": "https://example.org/version"},
        ),
    )
    failure = SourceVersionCheck(
        "check-2",
        dataset,
        checked_at + timedelta(minutes=1),
        VersionCheckOutcome.FAILED,
        error="SourceAcquisitionError: unavailable",
    )

    store.append(success)
    store.append(failure)

    history = store.latest_for((dataset,))[dataset]
    assert history.latest_attempt == failure
    assert history.latest_success == success


def test_invalid_persisted_check_is_reported_as_operational_state_failure(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "registry.sqlite3"
    store = SQLiteSourceVersionCheckStore(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO source_version_checks (
                check_id, source, dataset, outcome, checked_at, error
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "broken",
                "example",
                "records",
                "not-an-outcome",
                datetime.now(UTC).isoformat(),
                "broken",
            ),
        )

    with pytest.raises(OperationalStateUnavailableError, match="invalid data"):
        store.latest_for((DatasetId("example", "records"),))
