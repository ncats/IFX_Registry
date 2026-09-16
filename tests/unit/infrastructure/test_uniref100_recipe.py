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
from ifx_registry.infrastructure.recipes.uniref100 import UniRef100MembershipsRecipe
from ifx_registry.infrastructure.uniprot_sparql import (
    SparqlAttempt,
    SparqlResponse,
    UniProtSparqlError,
)


def _binding(**values: str) -> dict[str, dict[str, str]]:
    return {name: {"value": value} for name, value in values.items()}


class FakeSparqlClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sparql: str, *, timeout: float) -> SparqlResponse:
        assert timeout == 19
        self.queries.append(sparql)
        if "pav/version" in sparql:
            bindings = [_binding(version="2026_03")]
        else:
            bindings = []
            if "/P00001>" in sparql:
                bindings.append(
                    _binding(
                        protein="http://purl.uniprot.org/uniprot/P00001",
                        cluster="http://purl.uniprot.org/uniref/UniRef100_P00001",
                        identity="1.0",
                        isSeed="true",
                        isRepresentative="true",
                    )
                )
        return SparqlResponse(
            {"results": {"bindings": bindings}},
            (SparqlAttempt(datetime(2026, 9, 15, tzinfo=UTC), "200", False),),
            hashlib.sha256(sparql.encode()).hexdigest(),
        )


def _inputs(recipe, root: Path) -> dict[str, MaterializedRecipeInput]:
    protein_dir = root / "protein_ids"
    protein_dir.mkdir(parents=True)
    with (protein_dir / "protein_ids.tsv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            delimiter="\t",
            fieldnames=[
                "uniprot_id",
                "canonical_isoform_status",
                "canonical_ifx_id",
                "ncats_protein_id",
            ],
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "uniprot_id": "P00001",
                    "canonical_isoform_status": "canonical",
                    "canonical_ifx_id": "",
                    "ncats_protein_id": "IFXProtein:1",
                },
                {
                    "uniprot_id": "P00001-2",
                    "canonical_isoform_status": "noncanonical",
                    "canonical_ifx_id": "IFXProtein:1",
                    "ncats_protein_id": "IFXProtein:2",
                },
                {
                    "uniprot_id": "P00003",
                    "canonical_isoform_status": "canonical",
                    "canonical_ifx_id": "",
                    "ncats_protein_id": "IFXProtein:3",
                },
            ]
        )
    uniprot_dir = root / "uniprot"
    uniprot_dir.mkdir()
    slots = {slot.name: slot for slot in recipe.descriptor.inputs}
    protein_ref = RegisteredSnapshotRef(
        SnapshotRef.derived("target_graph:protein_ids:2.2.0-preuniref1"),
        "s3://bucket/protein_ids/manifest.yaml",
    )
    uniprot_ref = RegisteredSnapshotRef(
        SnapshotRef.source("uniprot:human_isoforms:2026_03-export1"),
        "s3://bucket/uniprot/manifest.yaml",
    )
    return {
        "protein_ids": MaterializedRecipeInput(
            slots["protein_ids"], protein_ref, protein_dir
        ),
        "uniprot_isoforms": MaterializedRecipeInput(
            slots["uniprot_isoforms"], uniprot_ref, uniprot_dir
        ),
    }


def test_builds_memberships_for_noncanonical_proteins_and_parents(
    tmp_path: Path,
) -> None:
    client = FakeSparqlClient()
    recipe = UniRef100MembershipsRecipe(
        client,
        batch_size=1,
        request_timeout=19,
        minimum_mapped_accessions=1,
        minimum_mapping_ratio=0.4,
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
            "uniprot_id": "P00001",
            "uniref100_cluster_id": "UniRef100_P00001",
            "uniref100_identity": "1.0",
            "uniref100_is_seed": "True",
            "uniref100_is_representative": "True",
        },
        {
            "uniprot_id": "P00001-2",
            "uniref100_cluster_id": "",
            "uniref100_identity": "",
            "uniref100_is_seed": "False",
            "uniref100_is_representative": "False",
        },
    ]
    assert product.validation["source_rows"] == 3
    assert product.validation["scoped_rows"] == 2
    assert product.validation["requested_accessions"] == 2
    assert product.validation["mapped_accessions"] == 1
    assert product.validation["mapping_ratio"] == 0.5
    assert product.observations[0].request_count == 4


class FailingSparqlClient:
    def __init__(self, status_code: int | None) -> None:
        self.status_code = status_code
        self.calls = 0

    def query(self, sparql: str, *, timeout: float) -> SparqlResponse:
        del timeout
        if "pav/version" in sparql:
            return SparqlResponse(
                {"results": {"bindings": [_binding(version="2026_03")]}},
                (SparqlAttempt(datetime(2026, 9, 15, tzinfo=UTC), "200", False),),
                hashlib.sha256(sparql.encode()).hexdigest(),
            )
        self.calls += 1
        raise UniProtSparqlError("service unavailable", status_code=self.status_code)


def test_service_failure_does_not_recursively_split_batches(tmp_path: Path) -> None:
    client = FailingSparqlClient(503)
    recipe = UniRef100MembershipsRecipe(
        client,
        batch_size=25,
        request_timeout=19,
        minimum_mapped_accessions=1,
    )

    import pytest

    with pytest.raises(InvalidDerivedBuildError, match="UniRef100 query failed"):
        recipe.build(
            _inputs(recipe, tmp_path / "inputs"),
            tmp_path / "output",
            NullProgressReporter(),
        )

    assert client.calls == 1


def test_rejects_implausibly_incomplete_memberships(tmp_path: Path) -> None:
    import pytest

    client = FakeSparqlClient()
    recipe = UniRef100MembershipsRecipe(
        client,
        batch_size=1,
        request_timeout=19,
        minimum_mapped_accessions=2,
        minimum_mapping_ratio=0.75,
    )

    with pytest.raises(InvalidDerivedBuildError, match="incomplete"):
        recipe.build(
            _inputs(recipe, tmp_path / "inputs"),
            tmp_path / "output",
            NullProgressReporter(),
        )
