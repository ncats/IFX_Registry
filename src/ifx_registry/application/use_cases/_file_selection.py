"""Shared named-file selection for file-backed Registry snapshots."""

from ifx_registry.domain.catalog import PublishedDatasetSnapshot, PublishedFile
from ifx_registry.domain.errors import DatasetFileNotFoundError, MultipleDatasetFilesError


def select_file(
    snapshot: PublishedDatasetSnapshot,
    file_name: str | None,
) -> PublishedFile:
    available = tuple(str(file.relative_path) for file in snapshot.files)
    if file_name is None:
        if len(snapshot.files) != 1:
            raise MultipleDatasetFilesError(
                f"Dataset {snapshot.snapshot_id} has {len(snapshot.files)} files; "
                f"choose one of: {', '.join(available)}"
            )
        return snapshot.files[0]
    match = next(
        (file for file in snapshot.files if str(file.relative_path) == file_name),
        None,
    )
    if match is None:
        raise DatasetFileNotFoundError(
            f"Dataset {snapshot.snapshot_id} does not declare {file_name!r}; "
            f"choose one of: {', '.join(available)}"
        )
    return match
