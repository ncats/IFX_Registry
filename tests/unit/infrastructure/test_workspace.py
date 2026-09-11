"""Tests for atomic source snapshot staging."""

from pathlib import Path

import pytest

from ifx_registry.domain.errors import SourceAcquisitionError
from ifx_registry.infrastructure.workspace import SnapshotWorkspace


def test_workspace_commits_complete_directory(tmp_path: Path) -> None:
    final_path = tmp_path / "source" / "dataset" / "1"

    with SnapshotWorkspace(final_path) as workspace:
        (workspace.staging_path / "data.txt").write_text("complete")
        workspace.commit()

    assert (final_path / "data.txt").read_text() == "complete"


def test_workspace_removes_staging_directory_after_error(tmp_path: Path) -> None:
    final_path = tmp_path / "source" / "dataset" / "1"

    with pytest.raises(RuntimeError, match="validation failed"):
        with SnapshotWorkspace(final_path) as workspace:
            (workspace.staging_path / "data.txt").write_text("incomplete")
            raise RuntimeError("validation failed")

    assert not final_path.exists()
    assert not list(final_path.parent.glob("*.part"))


def test_workspace_refuses_to_replace_existing_snapshot(tmp_path: Path) -> None:
    final_path = tmp_path / "source" / "dataset" / "1"
    final_path.mkdir(parents=True)

    with SnapshotWorkspace(final_path) as workspace:
        with pytest.raises(SourceAcquisitionError, match="immutable snapshot"):
            workspace.commit()
