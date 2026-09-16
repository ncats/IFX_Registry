"""Reusable SureChEMBL patent-family projection."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

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

_OUTPUT_FILE = "protein_patent_family_mentions.parquet"
_RECIPE_DIGEST = hashlib.sha256(
    b"ifx-registry:surechembl-patent-family-mentions:v2"
).hexdigest()
_UNIPROT_ACCESSION_RE = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9])"
    r"(?:-\d+)?$"
)


class SurechemblPatentFamilyMentionsRecipe(DerivedRecipe):
    @property
    def descriptor(self) -> DerivedRecipeDescriptor:
        return DerivedRecipeDescriptor(
            dataset=DatasetId("surechembl", "patent_family_mentions"),
            display_name="SureChEMBL patent-family mentions",
            description=(
                "Aggregates SureChEMBL protein mentions into reusable patent-family "
                "sets from one exact Patent Discovery snapshot."
            ),
            revision="2",
            inputs=(
                RecipeInputSlot(
                    "patent_discovery",
                    "SureChEMBL Patent Discovery",
                    SnapshotKind.SOURCE,
                    DatasetId("surechembl", "patent_discovery"),
                ),
            ),
            producer=ProducerIdentity(
                "ifx_registry",
                "0.2.0",
                "https://github.com/ncats/IFX_Registry",
                f"sha256:{_RECIPE_DIGEST}",
            ),
            transform={
                "name": "surechembl_patent_family_mentions",
                "version": 2,
                "min_publication_year": 1950,
                "max_publication_year": "input_snapshot_year",
            },
        )

    def build(
        self,
        inputs: Mapping[str, MaterializedRecipeInput],
        destination: Path,
        progress: ProgressReporter,
    ) -> DerivedRecipeProduct:
        source = inputs["patent_discovery"]
        version = source.reference.ref.version.value
        if not re.match(r"^\d{4}", version):
            raise InvalidDerivedBuildError(
                "SureChEMBL source version must begin with a four-digit year"
            )
        max_year = int(version[:4])
        source_dir = _required_directory(source)
        progress.report(ProgressUpdate("building", "Loading protein entity identifiers"))
        entity_targets, entity_type_column = _load_entity_targets(
            source_dir / "biomedical_entities.parquet"
        )
        if not entity_targets:
            raise InvalidDerivedBuildError(
                "SureChEMBL input contains no supported protein entity identifiers"
            )
        progress.report(ProgressUpdate("building", "Locating patents with protein mentions"))
        locations_file = source_dir / "biomedical_locations.parquet"
        relevant_patent_ids = _collect_relevant_patent_ids(
            locations_file,
            entity_targets,
        )
        progress.report(ProgressUpdate("building", "Loading patent-family metadata"))
        patent_metadata = _load_patent_metadata(
            source_dir / "patents.parquet",
            relevant_patent_ids,
            min_publication_year=1950,
            max_publication_year=max_year,
        )
        progress.report(ProgressUpdate("building", "Aggregating patent-family mentions"))
        rows = _mention_rows(locations_file, entity_targets, patent_metadata)
        if not rows:
            raise InvalidDerivedBuildError(
                "SureChEMBL input produced no protein patent-family mentions"
            )
        destination.mkdir(parents=True, exist_ok=True)
        output_path = destination / _OUTPUT_FILE
        table = pa.Table.from_pylist(
            rows,
            schema=pa.schema(
                [
                    ("protein_id", pa.string()),
                    ("patent_family_mentions", pa.list_(pa.string())),
                    ("patent_identifier_sources", pa.list_(pa.string())),
                ]
            ),
        )
        pq.write_table(table, output_path)
        return DerivedRecipeProduct(
            files=(
                DerivedSnapshotFile(
                    output_path,
                    PurePosixPath(_OUTPUT_FILE),
                    "application/vnd.apache.parquet",
                ),
            ),
            validation={
                "row_count": len(rows),
                "entity_target_count": len(entity_targets),
                "relevant_patent_count": len(relevant_patent_ids),
                "patent_metadata_count": len(patent_metadata),
                "min_publication_year": 1950,
                "max_publication_year": max_year,
                "entity_type_column": entity_type_column,
            },
        )


def _required_directory(item: MaterializedRecipeInput) -> Path:
    if item.local_directory is None:
        raise InvalidDerivedBuildError("SureChEMBL input must contain registered files")
    required = (
        "patents.parquet",
        "biomedical_entities.parquet",
        "biomedical_locations.parquet",
    )
    missing = [name for name in required if not (item.local_directory / name).is_file()]
    if missing:
        raise InvalidDerivedBuildError(
            f"SureChEMBL input is missing required files: {', '.join(missing)}"
        )
    return item.local_directory


def _normalize_resolved_form(value: object) -> tuple[str, str] | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized.startswith("HGNC:"):
        return normalized, "HGNC"
    if _UNIPROT_ACCESSION_RE.fullmatch(normalized):
        return f"UniProtKB:{normalized}", "UniProtKB"
    return None


def _load_entity_targets(path: Path) -> tuple[dict[int, tuple[str, str]], str]:
    targets: dict[int, tuple[str, str]] = {}
    parquet = pq.ParquetFile(path)
    columns = set(parquet.schema_arrow.names)
    entity_type_column = next(
        (name for name in ("type_id", "entity_type_id") if name in columns),
        None,
    )
    if entity_type_column is None:
        available = ", ".join(sorted(columns)) or "none"
        raise InvalidDerivedBuildError(
            "SureChEMBL biomedical_entities.parquet must contain type_id "
            f"(current) or entity_type_id (legacy); available columns: {available}"
        )
    for batch in parquet.iter_batches(columns=["id", entity_type_column, "resolved_form"]):
        for entity_id, entity_type_id, resolved_form in zip(
            batch.column("id").to_pylist(),
            batch.column(entity_type_column).to_pylist(),
            batch.column("resolved_form").to_pylist(),
            strict=True,
        ):
            if entity_type_id != 1:
                continue
            normalized = _normalize_resolved_form(resolved_form)
            if normalized is not None:
                targets[int(entity_id)] = normalized
    return targets, entity_type_column


def _location_batches(path: Path) -> Iterator[Any]:
    yield from pq.ParquetFile(path).iter_batches(columns=["patent_id", "entity_id"])


def _collect_relevant_patent_ids(
    path: Path,
    entity_targets: Mapping[int, tuple[str, str]],
) -> set[int]:
    patent_ids: set[int] = set()
    for batch in _location_batches(path):
        for patent_id, entity_id in zip(
            batch.column("patent_id").to_pylist(),
            batch.column("entity_id").to_pylist(),
            strict=True,
        ):
            if int(entity_id) in entity_targets:
                patent_ids.add(int(patent_id))
    return patent_ids


def _load_patent_metadata(
    path: Path,
    relevant_patent_ids: set[int],
    *,
    min_publication_year: int,
    max_publication_year: int,
) -> dict[int, tuple[int, int]]:
    metadata: dict[int, tuple[int, int]] = {}
    if not relevant_patent_ids:
        return metadata
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(columns=["id", "publication_date", "family_id"]):
        for patent_id, publication_date, family_id in zip(
            batch.column("id").to_pylist(),
            batch.column("publication_date").to_pylist(),
            batch.column("family_id").to_pylist(),
            strict=True,
        ):
            numeric_patent_id = int(patent_id)
            if numeric_patent_id not in relevant_patent_ids or family_id is None:
                continue
            numeric_family_id = int(family_id)
            year = publication_date.year if publication_date is not None else None
            if (
                numeric_family_id > 0
                and year is not None
                and min_publication_year <= year <= max_publication_year
            ):
                metadata[numeric_patent_id] = (numeric_family_id, year)
    return metadata


def _mention_rows(
    locations_file: Path,
    entity_targets: Mapping[int, tuple[str, str]],
    patent_metadata: Mapping[int, tuple[int, int]],
) -> list[dict[str, object]]:
    mention_map: dict[str, set[int]] = defaultdict(set)
    source_map: dict[str, set[str]] = defaultdict(set)
    for batch in _location_batches(locations_file):
        for patent_id, entity_id in zip(
            batch.column("patent_id").to_pylist(),
            batch.column("entity_id").to_pylist(),
            strict=True,
        ):
            normalized = entity_targets.get(int(entity_id))
            family_and_year = patent_metadata.get(int(patent_id))
            if normalized is None or family_and_year is None:
                continue
            target_id, source_type = normalized
            family_id, year = family_and_year
            mention_map[target_id].add((year << 32) | family_id)
            source_map[target_id].add(source_type)
    return [
        {
            "protein_id": protein_id,
            "patent_family_mentions": [
                f"{value >> 32}:{value & 0xFFFFFFFF}"
                for value in sorted(mention_map[protein_id])
            ],
            "patent_identifier_sources": sorted(source_map[protein_id]),
        }
        for protein_id in sorted(mention_map)
    ]
