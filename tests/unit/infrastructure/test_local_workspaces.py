"""Tests for disposable local job workspaces."""

from pathlib import Path

from ifx_registry.infrastructure.local_workspaces import LocalAcquisitionWorkspaceProvider


def test_cleanup_abandoned_removes_job_directories_only(tmp_path: Path) -> None:
    root = tmp_path / "work"
    abandoned = root / "job-123"
    abandoned.mkdir(parents=True)
    (abandoned / "partial.dat").write_bytes(b"partial")
    unrelated_directory = root / "not a job"
    unrelated_directory.mkdir()
    marker = root / "operator-note.txt"
    marker.write_text("keep")

    LocalAcquisitionWorkspaceProvider(root).cleanup_abandoned()

    assert not abandoned.exists()
    assert unrelated_directory.is_dir()
    assert marker.read_text() == "keep"


def test_cleanup_abandoned_accepts_missing_root(tmp_path: Path) -> None:
    LocalAcquisitionWorkspaceProvider(tmp_path / "missing").cleanup_abandoned()
