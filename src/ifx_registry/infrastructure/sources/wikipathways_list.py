"""Independently versioned WikiPathways live pathway catalog JSON."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

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
    require_download,
)
from ifx_registry.infrastructure.sources.version_strategies import SourceVersionStrategy

PATHWAY_LIST_URL = "https://www.wikipathways.org/json/listPathways.json"


def _profile(text: str) -> dict[str, int]:
    try:
        data = json.loads(text)
    except ValueError as error:
        raise SourceValidationError("WikiPathways list is not valid JSON") from error
    if not isinstance(data, dict) or not isinstance(data.get("organisms"), list):
        raise SourceValidationError("WikiPathways list lacks organisms")
    seen = set()
    human = 0
    for organism in data["organisms"]:
        if not isinstance(organism, dict) or not isinstance(organism.get("pathways"), list):
            raise SourceValidationError("Invalid WikiPathways organism record")
        for pathway in organism["pathways"]:
            if not isinstance(pathway, dict):
                raise SourceValidationError("Invalid WikiPathways pathway record")
            ident = pathway.get("id", "")
            if (
                not isinstance(ident, str)
                or not re.fullmatch(r"WP\d+", ident)
                or ident in seen
                or not isinstance(pathway.get("name"), str)
                or not pathway["name"].strip()
                or not isinstance(pathway.get("species"), str)
            ):
                raise SourceValidationError("Invalid or duplicate WikiPathways pathway")
            seen.add(ident)
            human += pathway["species"] == "Homo sapiens"
    if not human:
        raise SourceValidationError("WikiPathways list has no human pathways")
    return {"pathways": len(seen), "human_pathways": human, "organisms": len(data["organisms"])}


class WikiPathwaysListVersionStrategy(SourceVersionStrategy):
    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return (PATHWAY_LIST_URL,)

    @property
    def description(self) -> str:
        return (
            "Reads the small live pathway-list JSON and uses its content SHA-256 and "
            "HTTP update date; this is independent of the monthly GMT release."
        )

    def discover(self, http: HttpGateway, request: VersionProbeRequest) -> SourceVersion:
        response = http.get_text(PATHWAY_LIST_URL, timeout=request.timeout.total_seconds())
        _profile(response.text)
        modified = response.metadata.header("last-modified")
        if not modified:
            raise SourceValidationError("WikiPathways JSON has no Last-Modified header")
        updated = parse_http_date(modified, field_name="WikiPathways JSON Last-Modified")
        digest = hashlib.sha256(response.text.encode("utf-8")).hexdigest()
        return SourceVersion(
            f"{updated.isoformat()}-{digest[:16]}",
            version_date=updated,
            evidence={
                "sha256": digest,
                "last_modified": modified,
                "version_url": response.metadata.final_url,
                "release_relationship": "Live website export; not a monthly GMT release artifact",
            },
        )


class WikiPathwaysListSource(HttpSnapshotSource):
    @property
    def dataset(self) -> DatasetId:
        return DatasetId("wikipathways", "pathway_list")

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return (HttpFileSpec(PATHWAY_LIST_URL, "listPathways.json"),)

    @property
    def homepage(self) -> str:
        return "https://www.wikipathways.org/"

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return WikiPathwaysListVersionStrategy()

    def validate_downloads(
        self,
        version: SourceVersion,
        downloads: tuple[DownloadedSourceFile, ...],
    ) -> SourceValidationResult:
        payload = require_download(downloads, "listPathways.json").resource.path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != version.evidence["sha256"]:
            raise SourceValidationError("WikiPathways JSON changed between probe and download")
        metadata: dict[str, Any] = _profile(payload.decode("utf-8"))
        metadata["release_relationship"] = version.evidence["release_relationship"]
        return SourceValidationResult(version, metadata)
