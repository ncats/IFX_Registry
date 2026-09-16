"""Human-only projection of the NCBI gene identifier mapping bundle."""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from ifx_registry.application.derived_build_models import (
    DerivedRecipeProduct,
    MaterializedRecipeInput,
)
from ifx_registry.application.ports.derived_builds import DerivedRecipe
from ifx_registry.application.progress import ProgressReporter, ProgressUpdate
from ifx_registry.domain.derived_builds import DerivedRecipeDescriptor, RecipeInputSlot
from ifx_registry.domain.errors import InvalidDerivedBuildError, SourceValidationError
from ifx_registry.domain.models import (
    DatasetId,
    DerivedSnapshotFile,
    ProducerIdentity,
    SnapshotKind,
)
from ifx_registry.infrastructure.sources.ncbi_gene_mappings import (
    NCBI_GENE_MAPPING_FILES,
    NcbiMappingFile,
    profile_mapping_gzip,
)

_INPUT_SLOT = "gene_identifier_mappings"
_RECIPE_DIGEST = hashlib.sha256(
    b"ifx-registry:ncbi-human-gene-identifier-mappings:v1"
).hexdigest()


@dataclass(frozen=True, slots=True)
class MappingProjectionStats:
    source_rows: int
    selected_rows: int


class NcbiHumanGeneIdentifierMappingsRecipe(DerivedRecipe):
    """Select Homo sapiens rows without reshaping NCBI's source records."""

    def __init__(
        self,
        *,
        files: tuple[NcbiMappingFile, ...] = NCBI_GENE_MAPPING_FILES,
    ):
        if not files:
            raise ValueError("NCBI mapping recipe must contain at least one file")
        self._files = files

    @property
    def descriptor(self) -> DerivedRecipeDescriptor:
        return DerivedRecipeDescriptor(
            dataset=DatasetId("ncbi", "human_gene_identifier_mappings"),
            display_name="NCBI Human Gene Identifier Mappings",
            description=(
                "Human-only, byte-faithful rows from one exact NCBI Gene identifier "
                "mapping snapshot."
            ),
            revision="1",
            inputs=(
                RecipeInputSlot(
                    _INPUT_SLOT,
                    "NCBI Gene Identifier Mappings",
                    SnapshotKind.SOURCE,
                    DatasetId("ncbi", "gene_identifier_mappings"),
                ),
            ),
            producer=ProducerIdentity(
                "ifx_registry",
                "0.2.0",
                "https://github.com/ncats/IFX_Registry",
                f"sha256:{_RECIPE_DIGEST}",
            ),
            transform={
                "name": "ncbi_human_gene_identifier_mappings",
                "version": 1,
                "taxon_id": 9606,
                "record_handling": "preserve_header_and_selected_row_bytes",
                "gzip_mtime": 0,
            },
        )

    def build(
        self,
        inputs: Mapping[str, MaterializedRecipeInput],
        destination: Path,
        progress: ProgressReporter,
    ) -> DerivedRecipeProduct:
        source = inputs[_INPUT_SLOT]
        source_directory = _required_directory(source)
        destination.mkdir(parents=True, exist_ok=True)
        file_stats: dict[str, MappingProjectionStats] = {}
        output_files: list[DerivedSnapshotFile] = []

        for definition in self._files:
            progress.report(
                ProgressUpdate(
                    "building",
                    f"Selecting human rows from {definition.file.name}",
                )
            )
            input_path = source_directory / definition.file.name
            if not input_path.is_file():
                raise InvalidDerivedBuildError(
                    f"NCBI mapping input is missing {definition.file.name}"
                )
            output_path = destination / definition.file.name
            file_stats[definition.file.name] = _write_human_gzip(
                input_path,
                output_path,
                definition,
            )
            try:
                profile = profile_mapping_gzip(
                    output_path,
                    definition,
                    require_human_only=True,
                )
            except SourceValidationError as error:
                raise InvalidDerivedBuildError(str(error)) from error
            selected = profile["human_rows"]
            if not isinstance(selected, int):
                raise InvalidDerivedBuildError(
                    f"{definition.file.name} validation produced an invalid row count"
                )
            if selected < definition.minimum_human_rows:
                raise InvalidDerivedBuildError(
                    f"{definition.file.name} human row count is below the reviewed "
                    f"minimum: {selected} < {definition.minimum_human_rows}"
                )
            stats = file_stats[definition.file.name]
            if selected != stats.selected_rows:
                raise InvalidDerivedBuildError(
                    f"{definition.file.name} validation row count changed during build"
                )
            output_files.append(
                DerivedSnapshotFile(
                    output_path,
                    PurePosixPath(definition.file.name),
                    "application/gzip",
                )
            )

        try:
            version_date = date.fromisoformat(source.reference.ref.version.value)
        except ValueError as error:
            raise InvalidDerivedBuildError(
                "NCBI mapping source version must be an ISO calendar date"
            ) from error

        return DerivedRecipeProduct(
            files=tuple(output_files),
            validation={
                "taxon_id": 9606,
                "human_only": True,
                "files": {
                    definition.file.name: {
                        "source_rows": file_stats[definition.file.name].source_rows,
                        "selected_rows": file_stats[
                            definition.file.name
                        ].selected_rows,
                        "output_rows": file_stats[definition.file.name].selected_rows,
                        "columns": len(definition.header),
                        "expected_header": list(definition.header),
                    }
                    for definition in self._files
                },
                "total_source_rows": sum(
                    stats.source_rows for stats in file_stats.values()
                ),
                "total_rows": sum(
                    stats.selected_rows for stats in file_stats.values()
                ),
            },
            metadata={
                "source_snapshot_id": source.reference.snapshot_id,
                "record_handling": "preserve_header_and_selected_row_bytes",
            },
            version_date=version_date,
        )


def _required_directory(item: MaterializedRecipeInput) -> Path:
    if item.local_directory is None:
        raise InvalidDerivedBuildError(
            "NCBI mapping input must contain registered files"
        )
    return item.local_directory


def _write_human_gzip(
    input_path: Path,
    output_path: Path,
    definition: NcbiMappingFile,
) -> MappingProjectionStats:
    source_rows = 0
    selected = 0
    expected_tabs = len(definition.header) - 1
    try:
        with gzip.open(input_path, "rb") as source, output_path.open("wb") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_output,
                mtime=0,
            ) as output:
                header = next(source)
                observed_header = tuple(
                    value.decode("utf-8")
                    for value in header.rstrip(b"\r\n").split(b"\t")
                )
                if observed_header != definition.header:
                    raise InvalidDerivedBuildError(
                        f"{definition.file.name} header changed: "
                        + "\t".join(observed_header)
                    )
                output.write(header)
                for row_number, line in enumerate(source, start=2):
                    stripped = line.rstrip(b"\r\n")
                    if not stripped:
                        continue
                    source_rows += 1
                    if stripped.count(b"\t") != expected_tabs:
                        raise InvalidDerivedBuildError(
                            f"{definition.file.name} row {row_number} has the wrong "
                            "number of columns"
                        )
                    fields = stripped.split(b"\t", definition.taxon_index + 1)
                    taxon = fields[definition.taxon_index]
                    if not taxon.isdigit():
                        raise InvalidDerivedBuildError(
                            f"{definition.file.name} row {row_number} has an invalid "
                            "taxonomy ID"
                        )
                    if taxon == b"9606":
                        output.write(line)
                        selected += 1
    except StopIteration as error:
        raise InvalidDerivedBuildError(
            f"{definition.file.name} is empty"
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise InvalidDerivedBuildError(
            f"Could not filter {definition.file.name}: {error}"
        ) from error
    return MappingProjectionStats(source_rows, selected)
