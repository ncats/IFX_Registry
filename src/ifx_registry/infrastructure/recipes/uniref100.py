"""UniRef100 mappings projected from an exact UniProt human ID-mapping snapshot."""

from __future__ import annotations

import csv
import gzip
import hashlib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from ifx_registry.application.derived_build_models import (
    DerivedRecipeProduct,
    MaterializedRecipeInput,
)
from ifx_registry.application.ports.derived_builds import DerivedRecipe
from ifx_registry.application.progress import ProgressReporter, ProgressUpdate
from ifx_registry.domain.derived_builds import DerivedRecipeDescriptor, RecipeInputSlot
from ifx_registry.domain.errors import InvalidDerivedBuildError
from ifx_registry.domain.models import (
    DatasetId,
    DerivedSnapshotFile,
    ProducerIdentity,
    SnapshotKind,
)

_IDMAPPING_INPUT = "human_idmapping"
_IDMAPPING_FILE = "HUMAN_9606_idmapping.dat.gz"
_OUTPUT_FILE = "uniprot_uniref100_xref.csv"
_OUTPUT_COLUMNS = ("uniprot_id", "uniref100_cluster_id")
_DATABASE_NAME = "UniRef100"
_RECIPE_DIGEST = hashlib.sha256(b"ifx-registry:uniref100-memberships:v2").hexdigest()


class UniRef100MembershipsRecipe(DerivedRecipe):
    """Extract the release-coherent human UniRef100 accession mapping."""

    def __init__(self, *, minimum_mappings: int = 100_000):
        if minimum_mappings < 1:
            raise ValueError("minimum mappings must be positive")
        self._minimum_mappings = minimum_mappings

    @property
    def descriptor(self) -> DerivedRecipeDescriptor:
        return DerivedRecipeDescriptor(
            dataset=DatasetId("uniprot", "uniref100_memberships"),
            display_name="UniRef100 Protein Memberships",
            description=(
                "Projects human UniProt accession-to-UniRef100 cluster mappings "
                "from an exact UniProt ID-mapping snapshot."
            ),
            revision="2",
            inputs=(
                RecipeInputSlot(
                    _IDMAPPING_INPUT,
                    "UniProt Human ID Mapping",
                    SnapshotKind.SOURCE,
                    DatasetId("uniprot", "human_idmapping"),
                ),
            ),
            producer=ProducerIdentity(
                "ifx_registry",
                "0.2.0",
                "https://github.com/ncats/IFX_Registry",
                f"sha256:{_RECIPE_DIGEST}",
            ),
            transform={
                "name": "uniref100_memberships",
                "version": 2,
                "database": _DATABASE_NAME,
                "minimum_mappings": self._minimum_mappings,
            },
        )

    def build(
        self,
        inputs: Mapping[str, MaterializedRecipeInput],
        destination: Path,
        progress: ProgressReporter,
    ) -> DerivedRecipeProduct:
        source = _required_directory(inputs[_IDMAPPING_INPUT]) / _IDMAPPING_FILE
        if not source.is_file():
            raise InvalidDerivedBuildError(f"UniProt ID-mapping input is missing {_IDMAPPING_FILE}")

        progress.report(ProgressUpdate("building", "Extracting UniRef100 mappings"))
        mappings: dict[str, str] = {}
        source_rows = 0
        mapping_rows = 0
        duplicate_rows = 0
        try:
            with gzip.open(source, "rt", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    source_rows += 1
                    fields = line.rstrip("\r\n").split("\t")
                    if len(fields) != 3 or not all(fields):
                        raise InvalidDerivedBuildError(
                            f"UniProt ID-mapping line {line_number} does not contain "
                            "three non-empty fields"
                        )
                    accession, database, external_id = fields
                    if database != _DATABASE_NAME:
                        continue
                    mapping_rows += 1
                    previous = mappings.get(accession)
                    if previous is None:
                        mappings[accession] = external_id
                    elif previous == external_id:
                        duplicate_rows += 1
                    else:
                        raise InvalidDerivedBuildError(
                            "UniProt accession maps to conflicting UniRef100 clusters: "
                            f"{accession} -> {previous}, {external_id}"
                        )
        except InvalidDerivedBuildError:
            raise
        except (OSError, UnicodeDecodeError) as error:
            raise InvalidDerivedBuildError(f"Could not parse {_IDMAPPING_FILE}: {error}") from error

        if len(mappings) < self._minimum_mappings:
            raise InvalidDerivedBuildError(
                "UniProt ID mapping contains implausibly few UniRef100 mappings: "
                f"{len(mappings)} < {self._minimum_mappings}"
            )

        destination.mkdir(parents=True, exist_ok=True)
        output = destination / _OUTPUT_FILE
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(_OUTPUT_COLUMNS)
            writer.writerows(sorted(mappings.items()))

        return DerivedRecipeProduct(
            files=(DerivedSnapshotFile(output, PurePosixPath(_OUTPUT_FILE), "text/csv"),),
            validation={
                "source_rows": source_rows,
                "uniref100_rows": mapping_rows,
                "unique_accessions": len(mappings),
                "unique_clusters": len(set(mappings.values())),
                "duplicate_rows": duplicate_rows,
                "minimum_mappings": self._minimum_mappings,
            },
        )


def _required_directory(value: MaterializedRecipeInput) -> Path:
    if value.local_directory is None:
        raise InvalidDerivedBuildError(f"Input {value.slot.name} has no files")
    return value.local_directory
