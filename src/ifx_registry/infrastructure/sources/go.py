"""GO core OBO and basic JSON acquired as one ontology-versioned bundle."""

from __future__ import annotations

import json
import re
from datetime import date

from ifx_registry.application.contracts import VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
    require_download,
)
from ifx_registry.infrastructure.sources.version_strategies import SourceVersionStrategy

GO_BASE = "https://current.geneontology.org/ontology"
GO_FILES = (
    HttpFileSpec(f"{GO_BASE}/go-basic.json", "go-basic.json"),
    HttpFileSpec(f"{GO_BASE}/go.obo", "go.obo"),
)
GO_ROOTS = {"GO:0003674", "GO:0005575", "GO:0008150"}
BUNDLE_REVISION = "bundle1"


def obo_release(text: str) -> str:
    header = text.split("[Term]", 1)[0]
    versions: list[str] = re.findall(
        r"^data-version:\s*releases/(\d{4}-\d{2}-\d{2})\s*$", header, re.M
    )
    if len(versions) != 1:
        raise SourceValidationError("GO OBO header must declare exactly one release data-version")
    try:
        date.fromisoformat(versions[0])
    except ValueError as error:
        raise SourceValidationError("Invalid GO ontology release date") from error
    return versions[0]


class GoVersionStrategy(SourceVersionStrategy):
    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return (f"{GO_BASE}/go.obo",)

    @property
    def description(self) -> str:
        return "Reads GO's OBO data-version; bundle1 includes core OBO and basic JSON."

    def discover(self, http: HttpGateway, request: VersionProbeRequest) -> SourceVersion:
        response = http.get_text_prefix(
            self.evidence_urls[0],
            timeout=request.timeout.total_seconds(),
            max_bytes=65536,
        )
        release = obo_release(response.text)
        return SourceVersion(
            f"{release}-{BUNDLE_REVISION}",
            version_date=date.fromisoformat(release),
            evidence={
                "method": "go_obo_data_version",
                "ontology_release": release,
                "data_version": f"releases/{release}",
                "bundle_revision": BUNDLE_REVISION,
                "version_url": response.metadata.final_url,
            },
        )


class GoOntologySource(HttpSnapshotSource):
    def __init__(self, http: HttpGateway, *, minimum_terms: int = 30000):
        super().__init__(http)
        if minimum_terms < 1:
            raise ValueError("minimum_terms must be positive")
        self._minimum_terms = minimum_terms

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("go", "ontology")

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return GO_FILES

    @property
    def homepage(self) -> str:
        return "https://geneontology.org/"

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return GoVersionStrategy()

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        release = version.evidence["ontology_release"]
        obo = require_download(downloads, "go.obo").resource.path
        json_file = require_download(downloads, "go-basic.json").resource.path
        try:
            with obo.open(encoding="utf-8") as handle:
                header = handle.read(65536)
                handle.seek(0)
                ids: set[str] = set()
                in_term = False
                for line in handle:
                    if line.startswith("["):
                        in_term = line.strip() == "[Term]"
                    if in_term and line.startswith("id: "):
                        value = line[4:].strip()
                        if not re.fullmatch(r"GO:\d{7}", value) or value in ids:
                            raise SourceValidationError("GO OBO has invalid or duplicate term IDs")
                        ids.add(value)
            if obo_release(header) != release:
                raise SourceValidationError("GO OBO release changed before download")
            payload = json.loads(json_file.read_text(encoding="utf-8"))
            graphs = payload.get("graphs") if isinstance(payload, dict) else None
            if not isinstance(graphs, list) or len(graphs) != 1:
                raise SourceValidationError("GO JSON must contain one ontology graph")
            graph = graphs[0]
            expected = f"http://purl.obolibrary.org/obo/go/releases/{release}/go.owl"
            if graph.get("meta", {}).get("version") != expected:
                raise SourceValidationError("GO basic JSON and core OBO releases disagree")
            nodes = graph.get("nodes")
            edges = graph.get("edges")
            if not isinstance(nodes, list) or not isinstance(edges, list) or not edges:
                raise SourceValidationError("GO JSON lacks nodes or edges")
            json_ids = [
                node["id"].replace("http://purl.obolibrary.org/obo/GO_", "GO:")
                for node in nodes
                if node.get("type") == "CLASS"
                and node.get("id", "").startswith("http://purl.obolibrary.org/obo/GO_")
            ]
            if len(json_ids) != len(set(json_ids)):
                raise SourceValidationError("GO JSON contains duplicate terms")
            for label, terms in (("OBO", ids), ("JSON", set(json_ids))):
                if len(terms) < self._minimum_terms or not GO_ROOTS <= terms:
                    raise SourceValidationError(f"GO {label} has insufficient term/root coverage")
        except (ValueError, UnicodeError, KeyError, TypeError, AttributeError) as error:
            raise SourceValidationError("Invalid GO ontology payload") from error
        return SourceValidationResult(
            version,
            {
                "ontology_release": release,
                "obo_data_version": f"releases/{release}",
                "bundle_revision": BUNDLE_REVISION,
                "obo_terms": len(ids),
                "json_terms": len(json_ids),
                "json_edges": len(edges),
            },
        )
