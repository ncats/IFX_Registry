"""Fetch a source through its port and enforce the cross-source contract."""

from ifx_registry.application.contracts import FetchRequest
from ifx_registry.application.ports.source import SourceFetcher
from ifx_registry.application.versioning import ensure_expected_version
from ifx_registry.domain.errors import SourceContractError
from ifx_registry.domain.models import SourceSnapshot


class FetchSource:
    """Application service that validates every source adapter consistently."""

    def execute(self, source: SourceFetcher, request: FetchRequest) -> SourceSnapshot:
        snapshot = source.fetch(request)

        if snapshot.dataset != source.dataset:
            raise SourceContractError(
                f"Source adapter for {source.dataset} returned snapshot for {snapshot.dataset}"
            )

        ensure_expected_version(source.dataset, snapshot.version, request.expected_version)

        destination = request.destination.resolve()
        for snapshot_file in snapshot.files:
            local_path = snapshot_file.local_path.resolve()
            if not local_path.is_relative_to(destination):
                raise SourceContractError(
                    f"Source adapter for {source.dataset} returned file outside destination: "
                    f"{snapshot_file.local_path}"
                )
            if not local_path.is_file():
                raise SourceContractError(
                    f"Source adapter for {source.dataset} returned missing file: "
                    f"{snapshot_file.local_path}"
                )

        return snapshot
