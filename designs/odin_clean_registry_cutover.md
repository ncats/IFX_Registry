# IFX_ODIN Clean Registry Cutover

## Decision

IFX_Registry will become feature-complete before IFX_ODIN switches to it.
IFX_ODIN will then make one decisive cutover and remove its old Registry
implementation. We will not keep a permanent dual-client fallback.

IFX_Registry is a data catalog and immutable versioned snapshot store. It does
not own resolver behavior, graph semantics, graph databases, or graph lineage.
Those remain in IFX_ODIN, which is a caller of the Registry.

All existing `/registry*` routes will leave QA Browser. Dataset catalog and
registration views belong in the IFX_Registry web application. Resolver QA and
graph lineage that remain useful will use ODIN-oriented routes and names rather
than appearing to be Registry features.

## Registry Data Model

IFX_Registry persists only two concepts.

### Dataset snapshot

A `DatasetSnapshot` is immutable, exactly versioned, file-backed data. Every
snapshot contains at least one file and supports list, describe, materialize,
and checksum-verified caching.

A snapshot may be either:

- a **source snapshot**, with upstream location, version evidence, and download
  provenance; or
- a **derived snapshot**, with exact input snapshot IDs, producer/release and
  code identity, transformation metadata, and validation results.

Source versus derived describes provenance, not different storage or download
behavior. The package will reuse one internal repository and materializer while
keeping the kind explicit at the API and configuration boundary. It will not
guess by trying multiple S3 prefixes and catching lookup errors.

### External dataset version

An `ExternalDatasetVersion` is an immutable metadata assertion about an exact
database or API state that ODIN queries directly. It supports list and describe,
but never `file()` or `materialize()`.

It may record:

- provider, dataset, exact version, and optional version date;
- observation and registration timestamps;
- interface type such as MySQL, PostgreSQL, or GraphQL;
- a public or logical service name, access mode, version-check description,
  and sanitized evidence.

It must never contain passwords, tokens, access keys, usernames,
credential-file paths, internal URLs, role ARNs, or a promise that the Registry
can connect. ODIN receives actual connection settings independently.

## What Does Not Belong in IFX_Registry

### Resolvers

Resolver configuration and execution belong to ODIN. Current Registry resolver
snapshots are generally zero-file lockfiles containing an ODIN class path,
options, and exact dataset inputs; they are not datasets.

After cutover:

- ODIN YAML directly pins every dataset used by a resolver and records its
  options;
- the reviewed YAML is the resolver lockfile;
- ODIN build provenance records the input snapshot IDs, resolver configuration
  fingerprint, and ODIN revision; and
- resolver constructors receive materialized datasets and options directly.

If a resolver later produces a real reusable SQLite/index file, that file may
be registered as an ordinary derived dataset. The Registry still does not know
that ODIN uses it as a resolver.

No new `resolvers/` manifests will be created. Historical objects remain
untouched in S3 but become unreferenced after their exact inputs and options are
moved into ODIN YAML.

### Graphs

A live ArangoDB/MySQL graph, ETL run, resolver map, QA state, or operational
cache is not a Registry artifact. Graph build provenance and lineage remain in
ODIN and may link to datasets in the standalone Registry catalog.

A deliberately published portable export—TSV, JSONL, Parquet, or a database
dump—is ordinary derived data if another build needs to consume it. It is not a
special graph artifact, and Registry does not interpret its graph semantics.

## Ownership Boundary

IFX_Registry owns dataset identity, manifests, catalogs, source version checks,
downloads, checksum validation, immutable publication, S3 access, local
materialization, external version assertions, and its data-catalog web app.

IFX_ODIN owns graph ETL, adapters, resolvers, graph-specific transformations,
selection of exact dataset versions, resolver and graph provenance, external
system connectivity, and generic object storage used for graph parquet files,
workbooks, or curation data.

Reusable biomedical builders currently under `src/registry/derived/` are moved
into IFX_Registry as declared derived-data recipes after their code is separated
from ODIN types. Graph-specific builders remain in ODIN and call Registry
publication APIs when they produce reusable outputs. The generic S3 helper
currently under `src/registry/storage.py` is still relocated within ODIN for
non-Registry assets.

## Public Package Shape

Existing source consumption remains simple and source-specific:

```python
registry = RegistryClient.connect("aws_ifx_registry.yaml")
path = registry.file("reactome:pathways:97", "ReactomePathways.txt")
```

Derived calls explicitly identify their kind while reusing the same internal
file-backed machinery. External versions have explicit describe/list/register
methods and cannot be mistaken for local files. Producers explicitly publish
source or derived snapshots. IFX_Registry may also run its own declared reusable
recipes, but never runs ODIN graph builds or Harmonizer domain workflows.

S3 is the authoritative Registry. Local files are only verified materializations
or temporary publication workspaces; they are not an alternate Registry backend.

## Delivery Sequence

### 1. Close the current source milestone

- Commit and tag the tested source implementation as `v0.1.0`.
- Keep the 42 enabled automatic sources and all existing exact source snapshots
  readable, including manual captures without an enabled downloader.
- Do not add more fallback behavior to IFX_ODIN's current pilot integration.

### 2. Complete IFX_Registry's data contracts

- Generalize the internal file-backed snapshot contract without changing the
  source-only v0.1 API.
- Add derived manifest parsing, publication, describe, and materialization.
- Add manual/generated source publication.
- Add external dataset version registration, list, and describe.
- Reject secret-bearing external metadata.
- Separate source and derived cache paths so identical triples cannot collide.
- Preserve existing schema-version-1 manifests and S3 paths without rewriting
  or duplicating large objects.

There will be no resolver or graph API in IFX_Registry.

### 3. Complete the standalone data-catalog web application

- Show source and derived datasets from the bucket.
- Show versions, files, sizes, checksums, inputs, transformation identity,
  validation summaries, and provenance.
- Show external dataset versions clearly as metadata-only references.
- Support version checks and registration only for operations Registry owns.
- Do not show resolver catalogs, execute resolver classes, connect to graph
  databases, or present graph lineage as a Registry feature.

This replaces QA Browser's Registry dataset catalog and update/status UI.

### 4. Prepare ODIN consumers and producers before deleting legacy code

- Replace every live `resolver_snapshot:` with direct exact dataset pins and
  resolver options in ODIN YAML.
- Refactor resolver constructors to accept those materialized datasets and
  options directly. Preserve a deterministic resolver configuration/input
  fingerprint in graph build provenance.
- Retain useful resolver QA and graph lineage under ODIN-oriented routes; remove
  their dependency on Registry resolver manifests and link dataset IDs to the
  standalone Registry catalog.
- Remove the migrated PubChem and SureChEMBL builders from
  `src/registry/derived/`; migrate and then remove the remaining reusable ChEBI
  builder without changing scientific behavior.
- Change target, disease, and drug Harmonizer handoff scripts and other
  generated-data scripts to publish derived datasets instead of hand-building
  manifests.
- Change raw/manual capture scripts to call the source publisher.
- Remove resolver-registration paths from producer scripts.
- Change external-version scripts to submit safe metadata assertions; keep
  their database/API credentials in ODIN.
- Move graph/parquet/workbook object storage helpers to an ODIN-owned
  infrastructure module.

No legacy Registry code is deleted during this phase. Each replacement is
tested against sanitized existing manifests and exact production pins.

### 5. Tag the feature-complete Registry release

- Pass Registry tests, strict typing, lint, packaging, concurrency, and S3
  compatibility gates.
- Read every unique pinned source/derived/external reference in checked-in ODIN
  YAML through the new package.
- Compare representative results with the old implementation.
- Tag the complete package, expected to be `v0.2.0`.

### 6. Make one ODIN cutover

- Pin the immutable Registry tag in ODIN requirements and runtime images. This
  establishes Python 3.11 as the Registry-enabled ODIN baseline.
- Replace the source-only pilot with `src/core/registry_integration.py`.
- Let IFX_Registry parse its credential YAML. ODIN supplies only the path,
  cache root, and optional bucket override.
- Resolve exact source strings, explicit derived references, and explicit
  external version references. Do not guess kinds or fall back to old code.
- Adapt package results to one ODIN boundary object that supplies `file()`,
  `version_info()`, and provenance behavior expected by adapters.
- Reduce `src/core/config.py` to YAML loading, configured-object construction,
  and one Registry-resolution call. It must not parse AWS fields, translate
  credentials, infer artifact kinds, or route between implementations.
- Fail immediately when the package, credentials, kind, exact pin, or version
  is missing.

### 7. Delete the legacy implementation in the same ODIN change

Delete or replace:

- `src/core/data_registry.py`;
- the temporary `src/core/registry_source_client.py` pilot;
- Registry lifecycle code, manifests, source adapters, source configuration,
  and Registry-specific storage under `src/registry/`;
- resolver snapshot materialization, registration, fingerprint, and build-key
  helpers, plus `src/id_resolvers/resolver_snapshot.py` after constructor
  conversion;
- old Registry update and S3 diagnostic scripts after any still-useful command
  moves to IFX_Registry;
- all QA Browser `/registry*` routes, templates, catalog caches, update jobs,
  and Registry-only JavaScript/CSS;
- infrastructure tests whose behavior is now covered in IFX_Registry.

Retain only deliberately relocated ODIN code under accurate names: scientific
transformations, resolver execution and metadata, graph lineage, and generic
object storage for non-Registry assets.

## Configuration After Cutover

Source input:

```yaml
data_source: reactome:pathways:97
```

Derived input:

```yaml
data_source:
  kind: derived_snapshot
  snapshot_id: surechembl:patent_family_mentions:2026-06-01
```

Resolver configuration directly pins its data:

```yaml
class: TargetGraphProteinResolver
kwargs:
  data_source: target_graph:protein_ids:2026-08-31
  additional_ids_data_source: target_graph:uniprot_mapping:2026-08-31
```

External version provenance is explicit and separate from connection settings:

```yaml
data_source:
  kind: external_dataset_version
  snapshot_id: chembl:activity_database:chembl36
credentials: ./src/use_cases/secrets/pharos_credentials.yaml
```

Production has no implicit `latest`, no kind guessing, and no silent local
fallback.

## Cutover Gates

Deletion begins only when all of these are true:

1. Existing source, derived, and external manifests are readable through tested
   fixtures and the live bucket.
2. File-backed snapshots verify size and SHA-256, use kind-separated caches,
   and atomically repair corruption.
3. Publication is immutable, concurrent-safe, and idempotent only for identical
   content.
4. Derived provenance changes when an input or transformation identity changes.
5. External assertions reject secrets and cannot be materialized.
6. Every resolver formerly using a resolver snapshot has direct exact dataset
   pins and options in reviewed ODIN YAML, with stable provenance.
7. Representative source, derived, and external consumers work through the new
   package with no fallback.
8. Registry catalog/update UI has moved out of QA Browser; retained resolver QA
   and graph lineage are ODIN-owned and no longer use `/registry*` routes.
9. Every Registry-enabled ODIN runtime uses Python 3.11+ and installs the pinned
   package tag.
10. Focused tests and full suites pass in both repositories.
11. Static searches find no live `resolver_snapshot`, `DataRegistry`,
    `src.core.data_registry`, `src.registry`, optional Registry import, or
    exception-driven fallback.

The user runs representative Snakemake and ETL workflows for final acceptance,
including Pharos, resolver-heavy target graph, derived input, CURE, and Pounce
configurations.

## Data-Release Decision Kept Separate

Target, disease, and drug Harmonizer exports are derived datasets. A future
release should preferably package each coherent Harmonizer release as one
multi-file derived snapshot. Existing independently dated, source-kind exports
remain readable during the code cutover. Republishing or changing their pins is
a deliberate data-release decision, not a hidden code migration.

## Explicit Exclusions and Dependency

- IFX_Harmonizers is not changed during this cutover; the derived producer API
  is prepared for its later migration.
- RaMP acquisition remains out of scope; an already registered exact RaMP
  source snapshot remains consumable.
- Historical `resolvers/` S3 objects are retained but receive no replacement
  API and no new writes.
- Jess's local `impatient_target_graph` reconstruction is not edited or
  promoted without explicit approval. Because it currently uses resolver
  snapshots and legacy Registry code, the final deletion gate requires a
  decision to migrate or archive that scratch workflow. We will not retain
  `DataRegistry` solely as a hidden compatibility shim.

## Done Means

IFX_ODIN contains no Registry implementation and no Registry web application.
It contains one small integration boundary to the pinned IFX_Registry package,
plus ODIN-owned transformations, resolver behavior, graph lineage, and generic
object-storage code in accurately named modules. IFX_Registry is the only
implementation of dataset cataloging, version registration, immutable storage,
and retrieval.
