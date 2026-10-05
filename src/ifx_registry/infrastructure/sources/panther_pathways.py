"""Source-faithful PANTHER pathway associations and HMM classifications."""

from __future__ import annotations

import re

from ifx_registry.application.contracts import VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources._dates import parse_http_date
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
)
from ifx_registry.infrastructure.sources.version_strategies import SourceVersionStrategy

PATHWAY_INDEX = "https://data.pantherdb.org/ftp/pathway/current_release/"
HMM_INDEX = "https://data.pantherdb.org/ftp/hmm_classifications/current_release/"


def _latest(text: str, pattern: str) -> str:
    versions: list[str] = re.findall(pattern, text)
    if not versions:
        raise SourceValidationError("PANTHER listing does not contain the expected release file")
    return max(versions, key=lambda v: tuple(int(p) for p in v.split(".")))


def _files(version: SourceVersion) -> tuple[HttpFileSpec, ...]:
    pathway = f"SequenceAssociationPathway{version.evidence['pathway_release']}.txt"
    hmm = f"PANTHER{version.evidence['hmm_release']}_HMM_classifications"
    return (HttpFileSpec(PATHWAY_INDEX + pathway, pathway), HttpFileSpec(HMM_INDEX + hmm, hmm))


class PantherPathwayVersionStrategy(SourceVersionStrategy):
    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return (PATHWAY_INDEX, HMM_INDEX)

    @property
    def description(self) -> str:
        return "Reads the separate PANTHER pathway and HMM release filenames and file dates."

    def discover(self, http: HttpGateway, request: VersionProbeRequest) -> SourceVersion:
        timeout = request.timeout.total_seconds()
        pathway = _latest(
            http.get_text(PATHWAY_INDEX, timeout=timeout).text,
            r"SequenceAssociationPathway(\d+(?:\.\d+)+)\.txt",
        )
        hmm = _latest(
            http.get_text(HMM_INDEX, timeout=timeout).text,
            r"PANTHER(\d+(?:\.\d+)+)_HMM_classifications",
        )
        release = SourceVersion(
            f"{hmm}-pathway{pathway}",
            evidence={"pathway_release": pathway, "hmm_release": hmm},
        )
        dates = []
        file_dates = {}
        for spec in _files(release):
            metadata = http.head(spec.url, timeout=timeout)
            modified = metadata.header("last-modified")
            if not modified:
                raise SourceValidationError(f"Missing Last-Modified for {spec.name}")
            dates.append(parse_http_date(modified, field_name=spec.name))
            file_dates[spec.name] = modified
        return SourceVersion(
            release.value,
            version_date=max(dates),
            evidence={**release.evidence, "file_last_modified": file_dates},
        )


class PantherPathwaysSource(HttpSnapshotSource):
    @property
    def dataset(self) -> DatasetId:
        return DatasetId("panther", "pathways")

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return ()

    def file_specs_for(self, version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        return _files(version)

    @property
    def expected_file_count(self) -> int:
        return 2

    @property
    def homepage(self) -> str:
        return "https://pantherdb.org/"

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return PantherPathwayVersionStrategy()

    def validate_confirmation(
        self, version: SourceVersion, confirmed_version: SourceVersion
    ) -> None:
        super().validate_confirmation(version, confirmed_version)
        if (
            version.evidence["file_last_modified"]
            != confirmed_version.evidence["file_last_modified"]
        ):
            raise SourceValidationError("PANTHER files changed during acquisition")

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        counts = {}
        for downloaded in downloads:
            name = downloaded.spec.name
            if (
                downloaded.resource.metadata.header("last-modified")
                != version.evidence["file_last_modified"][name]
            ):
                raise SourceValidationError(f"PANTHER file changed during acquisition: {name}")
            is_pathway = name.startswith("SequenceAssociation")
            columns = 11 if is_pathway else 7
            rows = human_rows = 0
            with downloaded.resource.path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    fields = line.rstrip("\r\n").split("\t")
                    pattern = r"P\d+" if is_pathway else r"PTHR\d+(?::SF\d+)?"
                    if len(fields) != columns or not re.fullmatch(pattern, fields[0]):
                        raise SourceValidationError(f"Invalid PANTHER {name} row {rows + 1}")
                    rows += 1
                    if is_pathway:
                        human_rows += fields[4].startswith("HUMAN|")
            if not rows or (is_pathway and not human_rows):
                raise SourceValidationError(f"PANTHER {name} is empty or has no human associations")
            counts[name] = {"rows": rows, "human_rows": human_rows if is_pathway else None}
        return SourceValidationResult(version, {"file_counts": counts})
