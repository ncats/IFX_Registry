"""Transactional filesystem workspace for one immutable source snapshot."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from ifx_registry.domain.errors import SourceAcquisitionError


class SnapshotWorkspace:
    """Stage a complete snapshot and expose it only after validation succeeds."""

    def __init__(self, final_path: Path):
        self.final_path = Path(final_path)
        token = uuid4().hex
        self.staging_path = self.final_path.parent / f".{self.final_path.name}.{token}.part"
        self._committed = False

    def __enter__(self) -> SnapshotWorkspace:
        self.staging_path.mkdir(parents=True, exist_ok=False)
        return self

    def commit(self) -> Path:
        """Atomically make the fully prepared snapshot visible."""
        if self._committed:
            raise SourceAcquisitionError(f"Snapshot workspace already committed: {self.final_path}")
        if self.final_path.exists():
            raise SourceAcquisitionError(
                f"Refusing to replace existing immutable snapshot: {self.final_path}"
            )
        self.staging_path.replace(self.final_path)
        self._committed = True
        return self.final_path

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self._committed:
            shutil.rmtree(self.staging_path, ignore_errors=True)
