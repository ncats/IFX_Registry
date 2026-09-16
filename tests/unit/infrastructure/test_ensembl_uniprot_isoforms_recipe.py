from __future__ import annotations

import csv
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from ifx_registry.application.derived_build_models import MaterializedRecipeInput
from ifx_registry.application.progress import NullProgressReporter
from ifx_registry.domain.catalog import RegisteredSnapshotRef
from ifx_registry.domain.errors import InvalidDerivedBuildError
from ifx_registry.domain.models import SnapshotRef
from ifx_registry.infrastructure.recipes.ensembl_uniprot_isoforms import (
    EnsemblUniProtIsoformXrefsRecipe,
)
from ifx_registry.infrastructure.uniprot_sparql import (
    SparqlAttempt,
    SparqlResponse,
)


def _binding(**values: str) -> dict[str, dict[str, str]]:
    return {name: {"value": value} for name, value in values.items()}


class FakeSparqlClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sparql: str, *, timeout: float) -> SparqlResponse:
        assert timeout == 17
        self.queries.append(sparql)
        if "pav/version" in sparql:
            bindings = [_binding(version="2026_03")]
        else:
            bindings = []
            if "ENST000001.1" in sparql:
                bindings.append(
                    _binding(
                        ensemblTranscript=(
                            "http://rdf.ebi.ac.uk/resource/ensembl.transcript/"
                            "ENST000001.1"
                        ),
                        isoform="http://purl.uniprot.org/isoforms/P00001-1",
                    )
                )
        encoded = sparql.encode()
        return SparqlResponse(
            {"results": {"bindings": bindings}},
            (SparqlAttempt(datetime(2026, 9, 15, tzinfo=UTC), "200", False),),
            hashlib.sha256(encoded).hexdigest(),
        )


def _inputs(recipe, root: Path) -> dict[str, MaterializedRecipeInput]:
    ensembl_dir = root / "ensembl"
    ensembl_dir.mkdir(parents=True)
    with (ensembl_dir / "gene_transcript_identifiers.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Transcript stable ID version", "Protein stable ID version"],
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "Transcript stable ID version": "ENST000001.1",
                    "Protein stable ID version": "ENSP000001.1",
                },
                {
                    "Transcript stable ID version": "ENST000002.2",
                    "Protein stable ID version": "ENSP000002.2",
                },
                {
                    "Transcript stable ID version": "ENST000003.3",
                    "Protein stable ID version": "",
                },
            ]
        )
    uniprot_dir = root / "uniprot"
    uniprot_dir.mkdir()
    slots = {slot.name: slot for slot in recipe.descriptor.inputs}
    ensembl_ref = RegisteredSnapshotRef(
        SnapshotRef.source("ensembl:human_biomart:116-export1"),
        "s3://bucket/ensembl/manifest.yaml",
    )
    uniprot_ref = RegisteredSnapshotRef(
        SnapshotRef.source("uniprot:human_isoforms:2026_03-export1"),
        "s3://bucket/uniprot/manifest.yaml",
    )
    return {
        "ensembl_biomart": MaterializedRecipeInput(
            slots["ensembl_biomart"], ensembl_ref, ensembl_dir
        ),
        "uniprot_isoforms": MaterializedRecipeInput(
            slots["uniprot_isoforms"], uniprot_ref, uniprot_dir
        ),
    }


def test_builds_pinned_ensembl_uniprot_isoform_mapping(tmp_path: Path) -> None:
    client = FakeSparqlClient()
    recipe = EnsemblUniProtIsoformXrefsRecipe(
        client,
        batch_size=1,
        request_timeout=17,
        minimum_matched_transcripts=1,
    )

    product = recipe.build(
        _inputs(recipe, tmp_path / "inputs"),
        tmp_path / "output",
        NullProgressReporter(),
    )

    with product.files[0].local_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [
        {
            "ensembl_transcript_id_version": "ENST000001.1",
            "SPARQL_uniprot_isoform": "P00001-1",
        }
    ]
    assert product.validation == {
        "requested_transcripts": 2,
        "matched_transcripts": 1,
        "mapping_rows": 1,
        "mapping_ratio": 0.5,
        "minimum_matched_transcripts": 1,
        "minimum_mapping_ratio": 0.01,
        "uniprot_release": "2026_03",
        "query_scope": "peptide_bearing_transcripts",
    }
    assert product.observations[0].request_count == 4
    assert product.observations[0].http_status_counts == {"200": 4}
    assert len(client.queries) == 4


def test_rejects_implausibly_incomplete_mapping(tmp_path: Path) -> None:
    import pytest

    client = FakeSparqlClient()
    recipe = EnsemblUniProtIsoformXrefsRecipe(
        client,
        batch_size=1,
        request_timeout=17,
        minimum_matched_transcripts=2,
        minimum_mapping_ratio=0.75,
    )

    with pytest.raises(InvalidDerivedBuildError, match="incomplete"):
        recipe.build(
            _inputs(recipe, tmp_path / "inputs"),
            tmp_path / "output",
            NullProgressReporter(),
        )
