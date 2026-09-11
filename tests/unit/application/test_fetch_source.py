from pathlib import Path, PurePosixPath

import pytest

from ifx_registry.application.contracts import FetchRequest
from ifx_registry.application.ports.source import SourceFetcher
from ifx_registry.application.use_cases.fetch_source import FetchSource
from ifx_registry.domain.errors import SourceContractError, VersionMismatchError
from ifx_registry.domain.models import DatasetId, SnapshotFile, SourceSnapshot, SourceVersion


class FakeSource(SourceFetcher):
    def __init__(
        self,
        snapshot: SourceSnapshot,
        dataset: DatasetId | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._dataset = dataset or snapshot.dataset

    @property
    def dataset(self) -> DatasetId:
        return self._dataset

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        del request
        return self._snapshot


def make_snapshot(tmp_path: Path, version: str = "2026-09-08") -> SourceSnapshot:
    local_path = tmp_path / "example" / "records" / version / "records.tsv"
    local_path.parent.mkdir(parents=True)
    local_path.write_text("id\n1\n", encoding="utf-8")
    return SourceSnapshot(
        dataset=DatasetId(source="example", dataset="records"),
        version=SourceVersion(value=version),
        files=(
            SnapshotFile(
                local_path=local_path,
                relative_path=PurePosixPath("records.tsv"),
                source_url="https://example.org/records.tsv",
            ),
        ),
    )


def test_fetch_source_accepts_a_valid_adapter_result(tmp_path: Path) -> None:
    snapshot = make_snapshot(tmp_path)

    result = FetchSource().execute(
        FakeSource(snapshot),
        FetchRequest(
            destination=tmp_path,
            expected_version=SourceVersion(value="2026-09-08"),
        ),
    )

    assert result is snapshot


def test_fetch_source_rejects_wrong_dataset(tmp_path: Path) -> None:
    snapshot = make_snapshot(tmp_path)
    source = FakeSource(snapshot, dataset=DatasetId(source="other", dataset="records"))

    with pytest.raises(SourceContractError, match="returned snapshot"):
        FetchSource().execute(source, FetchRequest(destination=tmp_path))


def test_fetch_source_rejects_wrong_version(tmp_path: Path) -> None:
    snapshot = make_snapshot(tmp_path, version="2")

    with pytest.raises(VersionMismatchError, match="Expected"):
        FetchSource().execute(
            FakeSource(snapshot),
            FetchRequest(
                destination=tmp_path,
                expected_version=SourceVersion(value="1"),
            ),
        )


def test_fetch_source_rejects_files_outside_destination(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    outside = tmp_path / "outside.tsv"
    outside.write_text("id\n1\n", encoding="utf-8")
    snapshot = SourceSnapshot(
        dataset=DatasetId(source="example", dataset="records"),
        version=SourceVersion(value="1"),
        files=(
            SnapshotFile(
                local_path=outside,
                relative_path=PurePosixPath("outside.tsv"),
                source_url="https://example.org/outside.tsv",
            ),
        ),
    )

    with pytest.raises(SourceContractError, match="outside destination"):
        FetchSource().execute(FakeSource(snapshot), FetchRequest(destination=destination))
