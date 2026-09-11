"""Tests for the Registry-owned PubChem molecular-information recipe."""

import csv
import gzip
import json
import zipfile
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from ifx_registry.application.derived_build_models import MaterializedRecipeInput
from ifx_registry.application.progress import NullProgressReporter
from ifx_registry.domain.catalog import RegisteredSnapshotRef
from ifx_registry.domain.errors import InvalidDerivedBuildError
from ifx_registry.domain.models import SnapshotRef
from ifx_registry.infrastructure.recipes.pubchem import (
    PubchemBatchResult,
    PubchemCidMolecularInfoRecipe,
    PubchemCompoundCidSetRecipe,
    PubchemCompoundRecordsRecipe,
    PubchemRetryPolicy,
    PubchemServiceEvidence,
    RequestsPubchemCompoundClient,
)


def test_cid_set_recipe_combines_four_exact_registered_sources(tmp_path) -> None:
    recipe = PubchemCompoundCidSetRecipe()
    local_directories = {slot.name: tmp_path / slot.name for slot in recipe.descriptor.inputs}
    for directory in local_directories.values():
        directory.mkdir()
    with zipfile.ZipFile(
        local_directories["hmdb_metabolites"] / "hmdb_metabolites.zip", "w"
    ) as archive:
        archive.writestr(
            "hmdb_metabolites.xml",
            "<hmdb><metabolite><accession>0001</accession>"
            "<pubchem_compound_id>11</pubchem_compound_id></metabolite></hmdb>",
        )
    with zipfile.ZipFile(
        local_directories["wikipathways_rdf"] / "wikipathways_rdf_wp.zip", "w"
    ) as archive:
        archive.writestr(
            "pathway.ttl",
            "<https://identifiers.org/pubchem.compound/12> <p> <o> .",
        )
    with zipfile.ZipFile(
        local_directories["lipidmaps_structures"] / "LMSD.sdf.zip", "w"
    ) as archive:
        archive.writestr(
            "structures.sdf",
            "> <LM_ID>\nLMFA0001\n> <PUBCHEM_CID>\n13\n$$$$\n",
        )
    (local_directories["refmet_metabolites"] / "refmet.csv").write_text(
        "refmet_id,pubchem_cid\nR1,14\n",
        encoding="utf-8",
    )
    inputs = {}
    for slot in recipe.descriptor.inputs:
        reference = SnapshotRef.source(f"{slot.dataset}:1")
        inputs[slot.name] = MaterializedRecipeInput(
            slot,
            RegisteredSnapshotRef(reference, f"s3://registry/{slot.name}/manifest.yaml"),
            local_directories[slot.name],
        )

    product = recipe.build(inputs, tmp_path / "output", NullProgressReporter())

    with product.files[0].local_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert [row["cid"] for row in rows] == ["11", "12", "13", "14"]
    assert product.validation == {
        "row_count": 4,
        "distinct_cid_count": 4,
        "source_counts": {
            "hmdb": 1,
            "wikipathways": 1,
            "lipidmaps": 1,
            "refmet": 1,
        },
    }
def test_compound_records_recipe_fetches_batches_and_writes_a_manifest(tmp_path) -> None:
    class Client:
        calls: list[tuple[str, ...]] = []

        def fetch_batch(self, cids, *, timeout):  # type: ignore[no-untyped-def]
            assert timeout == 120
            self.calls.append(tuple(cids))
            return PubchemBatchResult(
                {"PC_Compounds": [{"id": {"id": {"cid": int(cid)}}} for cid in cids]},
                {cid: ("ok", "200", "") for cid in cids},
                PubchemServiceEvidence(
                    datetime(2026, 9, 10, 12, tzinfo=UTC),
                    datetime(2026, 9, 10, 12, 0, 1, tzinfo=UTC),
                    1,
                    0,
                    {"200": 1},
                    "green",
                ),
            )

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "pubchem_compound_cids.tsv").write_text(
        "pubchem_id\tcid\nPUBCHEM.COMPOUND:2\t2\nPUBCHEM.COMPOUND:1\t1\n",
        encoding="utf-8",
    )
    client = Client()
    recipe = PubchemCompoundRecordsRecipe(client)
    (cid_slot,) = recipe.descriptor.inputs
    reference = SnapshotRef.derived("pubchem:compound_cid_set:deps-1")
    registered = RegisteredSnapshotRef(reference, "s3://registry/manifest.yaml", "a" * 64)

    product = recipe.build(
        {cid_slot.name: MaterializedRecipeInput(cid_slot, registered, input_dir)},
        tmp_path / "output",
        NullProgressReporter(),
    )

    assert client.calls == [("1", "2")]
    assert [str(file.relative_path) for file in product.files] == [
        "pubchem_compound_records_manifest.tsv",
        "pubchem_compound_records_batch_000001.json.gz",
    ]
    with product.files[0].local_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert [row["cid"] for row in rows] == ["1", "2"]
    assert all(row["status"] == "ok" for row in rows)
    assert product.validation["ok_cid_count"] == 2
    assert len(product.observations) == 1
    assert product.observations[0].service_id == "pubchem:pug_rest"
    assert product.observations[0].request_count == 1

    repeated = recipe.build(
        {cid_slot.name: MaterializedRecipeInput(cid_slot, registered, input_dir)},
        tmp_path / "repeated-output",
        NullProgressReporter(),
    )
    assert product.files[1].local_path.read_bytes() == repeated.files[1].local_path.read_bytes()


def test_requests_client_retries_partial_success_then_fails_closed() -> None:
    class Response:
        status_code = 200
        text = ""
        headers: dict[str, str] = {}

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json():  # type: ignore[no-untyped-def]
            return {"PC_Compounds": [{"id": {"id": {"cid": 1}}}]}

    class Session:
        headers: dict[str, str] = {}
        calls = 0

        @staticmethod
        def get(url, *, timeout):  # type: ignore[no-untyped-def]
            assert url.endswith("/cid/1,2/JSON")
            assert timeout == 120
            Session.calls += 1
            return Response()

    sleeps: list[float] = []
    client = RequestsPubchemCompoundClient(  # type: ignore[arg-type]
        Session(),
        sleep=sleeps.append,
        jitter=lambda upper: 0,
    )

    with pytest.raises(InvalidDerivedBuildError, match="failed after 5 attempts"):
        client.fetch_batch(("1", "2"), timeout=120)

    assert Session.calls == 5
    assert sleeps == [0.25, 0.25, 0.25, 0.25]


def test_requests_client_honors_retry_after_and_records_evidence() -> None:
    class Response:
        text = ""

        def __init__(self, status_code, payload, headers=None):  # type: ignore[no-untyped-def]
            self.status_code = status_code
            self._payload = payload
            self.headers = headers or {}

        def json(self):  # type: ignore[no-untyped-def]
            return self._payload

    responses = [
        Response(429, {}, {"Retry-After": "2", "X-Throttling-Control": "Black"}),
        Response(
            200,
            {"PC_Compounds": [{"id": {"id": {"cid": 1}}}]},
            {"X-Throttling-Control": "Request Count status: Green (0%)"},
        ),
    ]

    class Session:
        headers: dict[str, str] = {}

        @staticmethod
        def get(url, *, timeout):  # type: ignore[no-untyped-def]
            return responses.pop(0)

    sleeps: list[float] = []
    client = RequestsPubchemCompoundClient(  # type: ignore[arg-type]
        Session(),
        sleep=sleeps.append,
        jitter=lambda upper: 0,
    )

    result = client.fetch_batch(("1",), timeout=120)

    assert result.statuses == {"1": ("ok", "200", "")}
    assert sleeps == [30.0, 0.25]
    assert result.evidence is not None
    assert result.evidence.request_count == 2
    assert result.evidence.retry_count == 1
    assert result.evidence.http_status_counts == {"429": 1, "200": 1}
    assert result.evidence.worst_throttle == "black"


def test_requests_client_does_not_expand_exhausted_throttling_to_individual_calls() -> None:
    class Response:
        status_code = 503
        text = "busy"
        headers = {"Retry-After": "1"}

    class Session:
        headers: dict[str, str] = {}
        urls: list[str] = []

        @staticmethod
        def get(url, *, timeout):  # type: ignore[no-untyped-def]
            Session.urls.append(url)
            return Response()

    client = RequestsPubchemCompoundClient(  # type: ignore[arg-type]
        Session(),
        policy=PubchemRetryPolicy(max_attempts=3),
        sleep=lambda seconds: None,
        jitter=lambda upper: 0,
    )

    with pytest.raises(InvalidDerivedBuildError, match="failed after 3 attempts"):
        client.fetch_batch(("1", "2"), timeout=120)

    assert len(Session.urls) == 3
    assert all(url.endswith("/cid/1,2/JSON") for url in Session.urls)


def test_requests_client_honors_http_date_retry_after() -> None:
    observed_at = datetime(2026, 9, 10, 12, tzinfo=UTC)

    class Response:
        text = ""

        def __init__(self, status_code, payload, headers=None):  # type: ignore[no-untyped-def]
            self.status_code = status_code
            self._payload = payload
            self.headers = headers or {}

        def json(self):  # type: ignore[no-untyped-def]
            return self._payload

    responses = [
        Response(503, {}, {"Retry-After": "Thu, 10 Sep 2026 12:00:07 GMT"}),
        Response(200, {"PC_Compounds": [{"id": {"id": {"cid": 1}}}]}),
    ]

    class Session:
        headers: dict[str, str] = {}

        @staticmethod
        def get(url, *, timeout):  # type: ignore[no-untyped-def]
            return responses.pop(0)

    sleeps: list[float] = []
    client = RequestsPubchemCompoundClient(  # type: ignore[arg-type]
        Session(),
        sleep=sleeps.append,
        clock=lambda: observed_at,
        jitter=lambda upper: 0,
    )

    client.fetch_batch(("1",), timeout=120)

    assert sleeps == [7.0, 0.25]


def test_requests_client_fails_immediately_on_permanent_batch_error() -> None:
    class Response:
        status_code = 400
        text = "bad request"
        headers: dict[str, str] = {}

    class Session:
        headers: dict[str, str] = {}
        calls = 0

        @staticmethod
        def get(url, *, timeout):  # type: ignore[no-untyped-def]
            Session.calls += 1
            return Response()

    client = RequestsPubchemCompoundClient(Session())  # type: ignore[arg-type]

    with pytest.raises(InvalidDerivedBuildError, match="permanently with HTTP 400"):
        client.fetch_batch(("1",), timeout=120)

    assert Session.calls == 1


def test_recipe_projects_compound_records_to_a_validated_tsv(tmp_path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "pubchem_compound_records_manifest.tsv").write_text(
        "batch_file\tstatus\nrecords.json.gz\tok\n",
        encoding="utf-8",
    )
    payload = {
        "PC_Compounds": [
            {
                "id": {"id": {"cid": 123}},
                "props": [
                    {
                        "urn": {"label": "InChIKey"},
                        "value": {"sval": "AAAA-BBBB"},
                    },
                    {
                        "urn": {"label": "Molecular Formula"},
                        "value": {"sval": "H2O"},
                    },
                ],
            }
        ]
    }
    with gzip.open(input_dir / "records.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    recipe = PubchemCidMolecularInfoRecipe()
    slot = recipe.descriptor.inputs[0]
    reference = SnapshotRef.derived("pubchem:compound_records:2026-09")
    registered = RegisteredSnapshotRef(
        reference,
        "s3://registry/derived/pubchem/compound_records/2026-09/manifest.yaml",
        "a" * 64,
    )

    product = recipe.build(
        {slot.name: MaterializedRecipeInput(slot, registered, input_dir)},
        tmp_path / "output",
        NullProgressReporter(),
    )

    assert product.files[0].relative_path == PurePosixPath("cid_molecular_info.tsv")
    with product.files[0].local_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert rows == [
        {
            "pubchem_id": "PUBCHEM.COMPOUND:123",
            "cid": "123",
            "monoisotopic_mass": "",
            "inchikey": "AAAA-BBBB",
            "inchi_key_prefix": "AAAA",
            "molecular_formula": "H2O",
            "molecular_weight": "",
            "canonical_smiles": "",
            "isomeric_smiles": "",
            "inchi": "",
            "iupac_name": "",
        }
    ]
    assert product.validation == {
        "row_count": 1,
        "with_inchikey_count": 1,
        "with_monoisotopic_mass_count": 0,
    }


@pytest.mark.parametrize("batch_file", ["../outside.json.gz", "/tmp/outside.json.gz"])
def test_recipe_rejects_unsafe_internal_manifest_paths(tmp_path, batch_file: str) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "pubchem_compound_records_manifest.tsv").write_text(
        f"batch_file\tstatus\n{batch_file}\tok\n",
        encoding="utf-8",
    )
    recipe = PubchemCidMolecularInfoRecipe()
    slot = recipe.descriptor.inputs[0]
    reference = SnapshotRef.derived("pubchem:compound_records:1")
    registered = RegisteredSnapshotRef(reference, "s3://registry/manifest.yaml", "a" * 64)

    with pytest.raises(InvalidDerivedBuildError, match="unsafe batch path"):
        recipe.build(
            {slot.name: MaterializedRecipeInput(slot, registered, input_dir)},
            tmp_path / "output",
            NullProgressReporter(),
        )


def test_recipe_rejects_a_manifest_without_successful_batches(tmp_path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "pubchem_compound_records_manifest.tsv").write_text(
        "batch_file\tstatus\nrecords.json.gz\terror\n",
        encoding="utf-8",
    )
    recipe = PubchemCidMolecularInfoRecipe()
    slot = recipe.descriptor.inputs[0]
    reference = SnapshotRef.derived("pubchem:compound_records:1")
    registered = RegisteredSnapshotRef(reference, "s3://registry/manifest.yaml", "a" * 64)

    with pytest.raises(InvalidDerivedBuildError, match="no successful record batches"):
        recipe.build(
            {slot.name: MaterializedRecipeInput(slot, registered, input_dir)},
            tmp_path / "output",
            NullProgressReporter(),
        )
