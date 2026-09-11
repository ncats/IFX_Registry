"""Disposable local working directories for source acquisition."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from ifx_registry.application.ports.snapshots import AcquisitionWorkspaceProvider

_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class LocalAcquisitionWorkspaceProvider(AcquisitionWorkspaceProvider):
    def __init__(self, root: Path):
        self._root = Path(root)

    def open(self, job_id: str) -> AbstractContextManager[Path]:
        if not _SAFE_JOB_ID.fullmatch(job_id):
            raise ValueError("job_id is not safe for use as a workspace name")
        return self._open(job_id)

    def cleanup_abandoned(self) -> None:
        """Remove job workspaces left behind when the service was interrupted."""
        if not self._root.exists():
            return
        for path in self._root.iterdir():
            if path.is_dir() and _SAFE_JOB_ID.fullmatch(path.name):
                shutil.rmtree(path, ignore_errors=True)

    @contextmanager
    def _open(self, job_id: str) -> Iterator[Path]:
        path = self._root / job_id
        path.mkdir(parents=True, exist_ok=False)
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)
