from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from ifx_registry.domain.errors import InvalidDatasetIdError
from ifx_registry.domain.models import DatasetId, SnapshotFile, SourceSnapshot, SourceVersion


def test_dataset_id_has_stable_string_form() -> None:
    assert str(DatasetId(source="uniprot", dataset="human_proteome")) == "uniprot:human_proteome"


@pytest.mark.parametrize("value", ["", "UniProt", "../uniprot", "uni/prot", "uni prot"])
def test_dataset_id_rejects_unsafe_source_names(value: str) -> None:
    with pytest.raises(InvalidDatasetIdError):
        DatasetId(source=value, dataset="human")


@pytest.mark.parametrize(
    "value",
    ["../../current", "release/current", "release:current", "release current"],
)
def test_source_version_rejects_unsafe_identifiers(value: str) -> None:
    with pytest.raises(ValueError, match="letters, numbers"):
        SourceVersion(value=value)


def test_snapshot_requires_unique_relative_file_paths(tmp_path: Path) -> None:
    version = SourceVersion(value="2026-09-08")
    duplicate = SnapshotFile(
        local_path=tmp_path / "records.tsv",
        relative_path=PurePosixPath("records.tsv"),
        source_url="https://example.org/records.tsv",
    )

    with pytest.raises(ValueError, match="unique"):
        SourceSnapshot(
            dataset=DatasetId(source="example", dataset="records"),
            version=version,
            files=(duplicate, duplicate),
            downloaded_at=datetime.now(UTC),
        )


def test_snapshot_id_combines_dataset_and_version(tmp_path: Path) -> None:
    local_path = tmp_path / "records.tsv"
    snapshot = SourceSnapshot(
        dataset=DatasetId(source="example", dataset="records"),
        version=SourceVersion(value="1.2.3"),
        files=(
            SnapshotFile(
                local_path=local_path,
                relative_path=PurePosixPath("records.tsv"),
                source_url="https://example.org/records.tsv",
            ),
        ),
    )

    assert snapshot.snapshot_id == "example:records:1.2.3"
