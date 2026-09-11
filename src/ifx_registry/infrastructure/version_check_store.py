"""SQLite persistence for upstream version-check observations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ifx_registry.application.ports.version_checks import SourceVersionCheckStore
from ifx_registry.domain.errors import OperationalStateUnavailableError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.domain.version_checks import (
    SourceCheckHistory,
    SourceVersionCheck,
    VersionCheckOutcome,
)


class SQLiteSourceVersionCheckStore(SourceVersionCheckStore):
    def __init__(self, database_path: Path):
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def append(self, check: SourceVersionCheck) -> None:
        version = check.version
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO source_version_checks (
                        check_id, source, dataset, outcome, checked_at,
                        version_value, version_date, discovered_at,
                        evidence_json, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        check.check_id,
                        check.dataset.source,
                        check.dataset.dataset,
                        check.outcome.value,
                        check.checked_at.isoformat(),
                        version.value if version else None,
                        version.version_date.isoformat()
                        if version and version.version_date
                        else None,
                        version.discovered_at.isoformat() if version else None,
                        json.dumps(version.evidence, sort_keys=True, default=str)
                        if version
                        else None,
                        check.error,
                    ),
                )
        except sqlite3.Error as error:
            raise OperationalStateUnavailableError(
                "Registry version-check history is temporarily unavailable"
            ) from error

    def latest_for(
        self,
        datasets: Iterable[DatasetId],
    ) -> Mapping[DatasetId, SourceCheckHistory]:
        requested = tuple(dict.fromkeys(datasets))
        if not requested:
            return {}
        conditions = " OR ".join("(source = ? AND dataset = ?)" for _ in requested)
        parameters = tuple(
            value for dataset in requested for value in (dataset.source, dataset.dataset)
        )
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT * FROM source_version_checks
                    WHERE {conditions}
                    ORDER BY checked_at DESC, check_id DESC
                    """,
                    parameters,
                ).fetchall()
        except sqlite3.Error as error:
            raise OperationalStateUnavailableError(
                "Registry version-check history is temporarily unavailable"
            ) from error

        attempts: dict[DatasetId, SourceVersionCheck] = {}
        successes: dict[DatasetId, SourceVersionCheck] = {}
        try:
            for row in rows:
                check = self._from_row(row)
                attempts.setdefault(check.dataset, check)
                if check.outcome is VersionCheckOutcome.SUCCEEDED:
                    successes.setdefault(check.dataset, check)
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise OperationalStateUnavailableError(
                "Registry version-check history contains invalid data"
            ) from error
        return {
            dataset: SourceCheckHistory(attempts.get(dataset), successes.get(dataset))
            for dataset in requested
            if dataset in attempts
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS source_version_checks (
                        check_id TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        dataset TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        checked_at TEXT NOT NULL,
                        version_value TEXT,
                        version_date TEXT,
                        discovered_at TEXT,
                        evidence_json TEXT,
                        error TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS source_version_checks_latest
                    ON source_version_checks (source, dataset, checked_at DESC, check_id DESC)
                    """
                )
        except sqlite3.Error as error:
            raise OperationalStateUnavailableError(
                "Registry version-check history is temporarily unavailable"
            ) from error

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SourceVersionCheck:
        version: SourceVersion | None = None
        if row["version_value"] is not None:
            evidence: Any = json.loads(str(row["evidence_json"] or "{}"))
            if not isinstance(evidence, dict):
                evidence = {}
            version = SourceVersion(
                value=str(row["version_value"]),
                version_date=(
                    date.fromisoformat(str(row["version_date"])) if row["version_date"] else None
                ),
                discovered_at=datetime.fromisoformat(str(row["discovered_at"])),
                evidence=evidence,
            )
        return SourceVersionCheck(
            check_id=str(row["check_id"]),
            dataset=DatasetId(str(row["source"]), str(row["dataset"])),
            checked_at=datetime.fromisoformat(str(row["checked_at"])),
            outcome=VersionCheckOutcome(str(row["outcome"])),
            version=version,
            error=str(row["error"]) if row["error"] else None,
        )
