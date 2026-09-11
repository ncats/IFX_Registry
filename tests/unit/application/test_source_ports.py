from ifx_registry.application.contracts import FetchRequest, VersionProbeRequest
from ifx_registry.application.ports.source import SourceAdapter, SourceFetcher
from ifx_registry.domain.models import DatasetId, SourceSnapshot, SourceVersion


class ManualSource(SourceFetcher):
    @property
    def dataset(self) -> DatasetId:
        return DatasetId(source="manual", dataset="records")

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        raise NotImplementedError


class AutomaticSource(SourceAdapter):
    @property
    def dataset(self) -> DatasetId:
        return DatasetId(source="automatic", dataset="records")

    @property
    def homepage(self) -> str:
        return "https://example.org/"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return ("https://example.org/records.tsv",)

    @property
    def version_check_description(self) -> str:
        return "Reads the example release metadata."

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return ("https://example.org/version",)

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        del request
        return SourceVersion(value="1")

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        raise NotImplementedError


def test_manual_source_only_needs_fetch_capability() -> None:
    source = ManualSource()

    assert isinstance(source, SourceFetcher)
    assert not isinstance(source, SourceAdapter)


def test_automatic_source_combines_probe_and_fetch_capabilities() -> None:
    source = AutomaticSource()

    assert isinstance(source, SourceFetcher)
    assert isinstance(source, SourceAdapter)
    assert source.discover_latest(VersionProbeRequest()).value == "1"
