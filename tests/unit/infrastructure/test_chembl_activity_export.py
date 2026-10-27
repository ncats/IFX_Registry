import csv
import gzip
import json
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from ifx_registry.infrastructure.sources import chembl_activity_export as export


def _fake_mysql(
    monkeypatch: pytest.MonkeyPatch, *, release="ChEMBL_37", columns=None,
    bounds=(1, 1), activity_ids=None,
):
    class FakeCursor:
        description = None

        def __init__(self):
            self.versions = [(release, datetime(2026, 5, 1))]
            self.bound = None
            self.seen_ranges = []
            self.rows = [tuple(
                "" if name == "compound_name" else None if name == "canonical_smiles"
                else 5.2 if name == "pchembl_value" else 38 if name == "src_id"
                else "value" for name in export.COLUMNS
            )]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, query, parameters=None):
            if query == export.RANGE_QUERY:
                self.description = [(name,) for name in (columns or export.COLUMNS)]
                self.seen_ranges.append((parameters["start"], parameters["end"]))
                if activity_ids is not None:
                    self.rows = [tuple(
                        activity_id if name == "activity_id" else ""
                        if name == "compound_name" else None
                        if name == "canonical_smiles" else 5.2
                        if name == "pchembl_value" else 38
                        if name == "src_id" else "value"
                        for name in export.COLUMNS
                    ) for activity_id in activity_ids
                        if parameters["start"] <= activity_id < parameters["end"]]
            elif query.endswith("ORDER BY activity_id LIMIT 1"):
                self.bound = bounds[0]
            elif query.endswith("ORDER BY activity_id DESC LIMIT 1"):
                self.bound = bounds[1]

        def fetchone(self):
            return (self.bound,)

        def fetchall(self):
            return [(self.bound,)] if self.bound is not None else self.versions

        def __iter__(self):
            return iter(self.rows)

    class FakeConnection:
        def __init__(self):
            self.cursor_instance = FakeCursor()
            self.rolled_back = False

        def cursor(self):
            return self.cursor_instance

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = FakeConnection()
    pymysql = types.ModuleType("pymysql")
    pymysql.connect = lambda **_: connection
    cursors = types.ModuleType("pymysql.cursors")
    cursors.SSCursor = object
    monkeypatch.setitem(sys.modules, "pymysql", pymysql)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", cursors)
    return connection


def _capture_files(directory: Path) -> None:
    (directory / export.FILE_NAME).write_bytes(b"activity_id\n1\n")
    (directory / export.SQL_NAME).write_text(export.RANGE_QUERY, encoding="utf-8")
    (directory / export.CONTRACT_NAME).write_text(
        json.dumps({
            "snapshot_id": "chembl:activity_exports:chembl37-export1",
            "database_release": "ChEMBL_37",
            "version_date": "2026-05-01",
            "captured_at": "2026-10-26T12:00:00+00:00",
            "pchembl_cutoff": 5,
            "null_encoding": "backslash_N_with_leading_backslash_escape",
            "row_count": 1,
            "patent_source_rows": 0,
            "rows_without_pubmed_id": 0,
            "query_sha256": export._sha256(directory / export.SQL_NAME),
            "data_sha256": export._sha256(directory / export.FILE_NAME),
        }),
        encoding="utf-8",
    )


def test_publish_capture_uses_verified_files_and_release_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_files(tmp_path)
    calls = []

    class FakeRegistry:
        def publish_source(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(export.RegistryClient, "connect", lambda _: FakeRegistry())
    result = export.publish_capture(tmp_path)
    assert result == "chembl:activity_exports:chembl37-export1"
    args, kwargs = calls[0]
    assert args == (result,)
    assert set(kwargs["files"]) == {
        export.FILE_NAME, export.SQL_NAME, export.CONTRACT_NAME,
    }
    assert kwargs["version_evidence"] == {"database_release": "ChEMBL_37"}
    assert kwargs["metadata"]["pchembl_cutoff"] == 5


def test_publish_capture_rejects_changed_export(tmp_path: Path) -> None:
    _capture_files(tmp_path)
    (tmp_path / export.FILE_NAME).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum differs"):
        export.publish_capture(tmp_path)


def test_capture_checks_release_and_preserves_null_vs_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _fake_mysql(monkeypatch)
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text("url: localhost\nuser: reader\n", encoding="utf-8")
    output = tmp_path / "capture"
    snapshot_id, contract = export.capture(
        credentials_file=credentials, schema="chembl37", output_dir=output,
    )
    assert snapshot_id == "chembl:activity_exports:chembl37-export1"
    assert contract["row_count"] == 1
    assert connection.rolled_back
    with gzip.open(output / export.FILE_NAME, "rt", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle, delimiter="\t"))
    assert row["compound_name"] == ""
    assert row["canonical_smiles"] == "\\N"
    assert export._encode_cell("\\N") == "\\\\N"


@pytest.mark.parametrize("release,columns,error", [
    ("ChEMBL_36", None, "must have one version row"),
    ("ChEMBL_37", ("wrong",), "columns differ"),
])
def test_capture_fails_on_wrong_release_or_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    release: str, columns: tuple[str, ...] | None, error: str,
) -> None:
    _fake_mysql(monkeypatch, release=release, columns=columns)
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text("url: localhost\nuser: reader\n", encoding="utf-8")
    output = tmp_path / "capture"
    with pytest.raises(ValueError, match=error):
        export.capture(
            credentials_file=credentials, schema="chembl37", output_dir=output,
        )
    assert not list(output.iterdir())


def test_capture_ranges_include_boundaries_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(export, "ACTIVITY_ID_CHUNK_SIZE", 3)
    connection = _fake_mysql(
        monkeypatch, bounds=(1, 7), activity_ids=(1, 3, 4, 7),
    )
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text("url: localhost\nuser: reader\n", encoding="utf-8")
    output = tmp_path / "capture"
    _, contract = export.capture(
        credentials_file=credentials, schema="chembl37", output_dir=output,
    )
    assert connection.cursor_instance.seen_ranges == [(1, 4), (4, 7), (7, 10)]
    assert contract["row_count"] == 4
    with gzip.open(output / export.FILE_NAME, "rt", encoding="utf-8") as handle:
        ids = [int(row["activity_id"]) for row in csv.DictReader(handle, delimiter="\t")]
    assert ids == [1, 3, 4, 7]
