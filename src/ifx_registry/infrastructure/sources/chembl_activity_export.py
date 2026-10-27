"""Repeatable, read-only capture of the Pharos ChEMBL activity projection."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ifx_registry.client import RegistryClient

EXPORT_REVISION = "export1"
PCHEMBL_CUTOFF = 5
FILE_NAME = "chembl_activities_pchembl5.tsv.gz"
SQL_NAME = "chembl_activities_pchembl5.sql"
CONTRACT_NAME = "chembl_activity_export_contract.json"
COLUMNS = (
    "activity_id", "compound_id", "compound_name", "canonical_smiles",
    "target_id", "uniprot_id", "pref_name", "action_type", "pchembl_value",
    "standard_type", "assay_type", "journal", "year", "volume", "issue",
    "first_page", "pubmed_id", "src_id",
)
NULL_TOKEN = "\\N"
ACTIVITY_ID_CHUNK_SIZE = 500_000

# Matches IFX_ODIN's ChemblAdapter.get_all_activities_query(5), including the
# molregno compound-record join and the document-or-patent selection rule.
QUERY = """SELECT DISTINCT
    activities.activity_id,
    molecule_dictionary.chembl_id AS compound_id,
    compound_records.compound_name,
    compound_structures.canonical_smiles,
    target_dictionary.chembl_id AS target_id,
    component_sequences.accession AS uniprot_id,
    target_dictionary.pref_name,
    activities.action_type,
    activities.pchembl_value,
    activities.standard_type,
    assays.assay_type,
    docs.journal,
    docs.year,
    docs.volume,
    docs.issue,
    docs.first_page,
    docs.pubmed_id,
    compound_records.src_id
FROM activities
JOIN compound_records ON activities.molregno = compound_records.molregno
JOIN compound_structures ON activities.molregno = compound_structures.molregno
JOIN molecule_dictionary ON activities.molregno = molecule_dictionary.molregno
JOIN assays ON activities.assay_id = assays.assay_id
JOIN target_dictionary ON assays.tid = target_dictionary.tid
JOIN target_components ON target_dictionary.tid = target_components.tid
JOIN component_sequences ON target_components.component_id = component_sequences.component_id
LEFT JOIN docs ON activities.doc_id = docs.doc_id
WHERE molecule_dictionary.structure_type = 'MOL'
  AND target_dictionary.target_type = 'SINGLE PROTEIN'
  AND component_sequences.tax_id = 9606
  AND activities.standard_flag = 1
  AND activities.pchembl_value >= 5
  AND activities.standard_relation = '='
  AND assays.assay_type IN ('F', 'B')
  AND (activities.doc_id IS NOT NULL OR compound_records.src_id = 38)
"""
RANGE_QUERY = (
    QUERY
    + "  AND activities.activity_id >= %(start)s"
    + " AND activities.activity_id < %(end)s\n"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _encode_cell(value: object) -> object:
    if value is None:
        return NULL_TOKEN
    if isinstance(value, str) and value.startswith("\\"):
        return "\\" + value
    return value


def capture(
    *, credentials_file: Path, schema: str, output_dir: Path,
) -> tuple[str, dict[str, object]]:
    """Capture one verified ChEMBL release without modifying the source DB."""
    try:
        import pymysql
        from pymysql.cursors import SSCursor
    except ImportError as error:
        raise RuntimeError("Install ifx-registry[sources] for ChEMBL capture") from error

    credentials = yaml.safe_load(credentials_file.read_text(encoding="utf-8"))
    if not isinstance(credentials, dict):
        raise ValueError("ChEMBL credentials must be a YAML mapping")
    if not schema.startswith("chembl") or not schema[6:].isdigit():
        raise ValueError("ChEMBL schema must have the form chembl<N>")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Capture directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    connection = pymysql.connect(
        host=credentials["url"], user=credentials["user"],
        password=credentials.get("password"), port=credentials.get("port") or 3306,
        database=schema, charset="utf8mb4", autocommit=False,
        read_timeout=3600, cursorclass=SSCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            expected_name = f"ChEMBL_{schema[6:]}"
            cursor.execute(
                "SELECT name, creation_date FROM version WHERE name = %s",
                (expected_name,),
            )
            versions = cursor.fetchall()
            matching = [row for row in versions if row[0] == expected_name]
            if len(matching) != 1 or len(versions) != 1 or matching[0][1] is None:
                raise ValueError(
                    f"{schema} must have one version row named {expected_name}; "
                    f"observed {[row[0] for row in versions]}"
                )
            version_date = matching[0][1].date()

            cursor.execute(
                "SELECT activity_id FROM activities ORDER BY activity_id LIMIT 1"
            )
            first_rows = cursor.fetchall()
            cursor.execute(
                "SELECT activity_id FROM activities ORDER BY activity_id DESC LIMIT 1"
            )
            last_rows = cursor.fetchall()
            if len(first_rows) != 1 or len(last_rows) != 1:
                raise ValueError(f"{schema} activities table is empty")
            first_activity_id = first_rows[0][0]
            last_activity_id = last_rows[0][0]
            if first_activity_id > last_activity_id:
                raise ValueError(f"{schema} activities has invalid ID bounds")

            captured_at = datetime.now(UTC)
            data_path = output_dir / FILE_NAME
            row_count = 0
            patent_source_rows = 0
            rows_without_pubmed_id = 0
            with gzip.open(data_path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, delimiter="\t")
                writer.writerow(COLUMNS)
                for start in range(
                    first_activity_id, last_activity_id + 1, ACTIVITY_ID_CHUNK_SIZE
                ):
                    end = start + ACTIVITY_ID_CHUNK_SIZE
                    cursor.execute(RANGE_QUERY, {"start": start, "end": end})
                    if tuple(item[0] for item in cursor.description or ()) != COLUMNS:
                        raise ValueError(
                            "ChEMBL query columns differ from the export contract"
                        )
                    chunk_rows = 0
                    for row in cursor:
                        writer.writerow([_encode_cell(value) for value in row])
                        row_count += 1
                        chunk_rows += 1
                        patent_source_rows += row[-1] == 38
                        rows_without_pubmed_id += row[-2] is None
                    print(f"Captured activity IDs [{start}, {end}): {chunk_rows} rows", flush=True)
            if row_count == 0:
                raise ValueError(f"{schema} ChEMBL activity query returned no rows")
        connection.rollback()
    except BaseException:
        try:
            connection.rollback()
        except Exception:
            pass  # Preserve the query/capture failure if the connection is lost.
        for name in (FILE_NAME, SQL_NAME, CONTRACT_NAME):
            (output_dir / name).unlink(missing_ok=True)
        raise
    finally:
        connection.close()

    sql_path = output_dir / SQL_NAME
    sql_path.write_text(RANGE_QUERY, encoding="utf-8")
    snapshot_id = f"chembl:activity_exports:{schema}-{EXPORT_REVISION}"
    contract: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "database_release": expected_name,
        "version_date": version_date.isoformat(),
        "captured_at": captured_at.isoformat(),
        "pchembl_cutoff": PCHEMBL_CUTOFF,
        "null_encoding": "backslash_N_with_leading_backslash_escape",
        "activity_id_min": first_activity_id,
        "activity_id_max": last_activity_id,
        "activity_id_chunk_size": ACTIVITY_ID_CHUNK_SIZE,
        "columns": list(COLUMNS),
        "row_count": row_count,
        "patent_source_rows": patent_source_rows,
        "rows_without_pubmed_id": rows_without_pubmed_id,
        "query_sha256": _sha256(sql_path),
        "data_sha256": _sha256(data_path),
    }
    (output_dir / CONTRACT_NAME).write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return snapshot_id, contract


def publish_capture(output_dir: Path, registry_credentials: Path | None = None) -> str:
    contract = json.loads((output_dir / CONTRACT_NAME).read_text(encoding="utf-8"))
    if contract.get("pchembl_cutoff") != PCHEMBL_CUTOFF:
        raise ValueError("ChEMBL export contract has an unsupported cutoff")
    if contract.get("null_encoding") != "backslash_N_with_leading_backslash_escape":
        raise ValueError("ChEMBL export contract has an unsupported NULL encoding")
    if contract.get("row_count", 0) <= 0:
        raise ValueError("ChEMBL export contract has no rows")
    if contract.get("query_sha256") != _sha256(output_dir / SQL_NAME):
        raise ValueError("ChEMBL export SQL checksum differs from its contract")
    if (output_dir / SQL_NAME).read_text(encoding="utf-8") != RANGE_QUERY:
        raise ValueError("ChEMBL export SQL differs from this export revision")
    if contract.get("data_sha256") != _sha256(output_dir / FILE_NAME):
        raise ValueError("ChEMBL export data checksum differs from its contract")
    snapshot_id = str(contract["snapshot_id"])
    release = str(contract["database_release"])
    expected_id = (
        f"chembl:activity_exports:{release.replace('_', '').lower()}-{EXPORT_REVISION}"
    )
    if snapshot_id != expected_id:
        raise ValueError("ChEMBL export snapshot ID differs from its database release")
    registry = RegistryClient.connect(registry_credentials)
    registry.publish_source(
        snapshot_id,
        files={name: output_dir / name for name in (FILE_NAME, SQL_NAME, CONTRACT_NAME)},
        captured_at=datetime.fromisoformat(str(contract["captured_at"])),
        capture_method="manual",
        version_date=datetime.fromisoformat(str(contract["version_date"])).date(),
        version_evidence={"database_release": release},
        validation={key: contract[key] for key in (
            "row_count", "patent_source_rows", "rows_without_pubmed_id",
            "data_sha256", "query_sha256",
        )},
        metadata={"pchembl_cutoff": PCHEMBL_CUTOFF},
    )
    return snapshot_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--schema", help="Exact ChEMBL schema, e.g. chembl37")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--publish-existing", action="store_true")
    parser.add_argument("--registry-credentials", type=Path)
    args = parser.parse_args()
    if args.publish_existing:
        snapshot_id = publish_capture(args.output_dir, args.registry_credentials)
        print(f"Published {snapshot_id}")
    else:
        if args.credentials is None or args.schema is None:
            parser.error("capture requires --credentials and --schema")
        snapshot_id, contract = capture(
            credentials_file=args.credentials, schema=args.schema,
            output_dir=args.output_dir,
        )
        print(f"Captured {snapshot_id}: {contract['row_count']} rows")


if __name__ == "__main__":
    main()
