# IFX Registry Separation

## Executive Summary

IFX_ODIN currently contains both graph-building code and a growing data
registry. The registry now manages source versions, downloads, shared files,
metadata, and a web catalog. These responsibilities are useful beyond ODIN and
are already needed by IFX_harmonizers.

We will move this shared functionality into the new IFX_Registry repository.
IFX_Registry will become the common place for acquiring, versioning, storing,
and finding data used across IFX projects.

The other repositories will remain focused on their core responsibilities:

- **IFX_Registry:** manages shared data and artifacts.
- **IFX_harmonizers:** cleans and harmonizes biomedical identifiers.
- **IFX_ODIN:** builds integrated graphs and application databases.

This is a good separation because the registry is now a shared service rather
than an internal detail of ODIN.

## Using a Pinned Dataset

The main consumer workflow is intentionally simple. A caller such as IFX_ODIN
provides:

- a pinned dataset ID, such as `source:reactome:pathways:97`;
- non-secret Registry settings, such as the S3 bucket and local cache
  directory; and
- an AWS identity with permission to read the Registry.

The caller asks the package for that dataset's file. IFX_Registry finds its
manifest, downloads any files not already present in the local cache, verifies
their checksums, and returns the local dataset directory and named files.

Normal use is two lines after import—one to connect and one to retrieve a
verified file:

```python
registry = RegistryClient.connect("aws_ifx_registry.yaml")
pathways_file = registry.file("reactome:pathways:97", "ReactomePathways.txt")
```

The cache directory and other infrastructure settings have safe defaults and
remain optional. Multi-file callers can use `registry.materialize(...)` when
they need the manifest or several named files.

ODIN then passes the returned local file to its existing adapter or resolver.
ODIN does not need to understand S3 paths, manifest layout, download behavior,
or checksum validation. It records the pinned dataset ID and manifest metadata
in its build provenance.

A missing, corrupt, or unauthorized dataset fails clearly. The package must
never replace a pinned version with `latest`. Changing data used by a build is
an explicit configuration change in the caller's repository.

## Registering a Derived Dataset

The corresponding producer workflow is used by IFX_harmonizers. After a
Harmonizer domain build succeeds, it provides:

- a new three-part derived dataset ID, such as
  `ifx_harmonizers:targets:2.2.0`;
- the output files belonging to that release;
- the exact pinned Registry datasets used as inputs;
- the Harmonizer release and code revision; and
- summary validation information, such as row counts or identifier coverage.

The package validates the request, calculates file checksums, creates the
manifest, and uploads the files and manifest as one immutable derived artifact.

Conceptually:

```python
registry.derived.publish(
    "ifx_harmonizers:targets:2.2.0",
    files={
        "gene_ids.tsv": gene_ids,
        "transcript_ids.tsv": transcript_ids,
        "protein_ids.tsv": protein_ids,
        "uniprot_mapping.csv": uniprot_mapping,
    },
    inputs=pinned_source_datasets,
    producer=producer_identity,
    transform=transform_identity,
    validation=release_summary,
)
```

IFX_harmonizers owns its domain build and scientific approval. IFX_Registry owns
validation of the artifact package, immutable storage, and subsequent
distribution. Separately, the Registry may run declared reusable derived-data
recipes that are useful across consumers; those recipes are Registry features,
not Harmonizer or graph builds. Once registered, ODIN can consume the entire
target release by its pinned ID and select the named files it needs.

## Why Make This Change

Today, related responsibilities are spread across repositories:

- ODIN contains the main registry implementation and registry web pages.
- IFX_harmonizers has many independent source download and version-checking
  workflows.
- Moving Harmonizer outputs into ODIN requires several staging and registration
  scripts.

This creates duplicated work, inconsistent behavior, and tight coupling between
projects. It also makes ODIN harder to understand and maintain.

A shared Registry will provide:

- one authoritative copy of each source version in the shared S3 bucket;
- consistent source version checks and downloads;
- reproducible inputs for all downstream builds;
- a simpler handoff from Harmonizers to ODIN;
- a common catalog showing what data is available; and
- clearer ownership across teams and repositories.

## Proposed Operating Model

IFX_Registry will provide a reusable Python package, a command-line tool, and a
small web application.

The package will initially be installed directly from a tagged GitHub release.
For example, another project can depend on Registry version `v0.2.0` through
its normal Python requirements file. This gives the projects a shared,
controlled version without waiting for formal package publication. Once the
package is stable, it can be published to an approved Python package index.

Registry updates will initially remain human-approved. Automated checks can
report when a new upstream version is available, but a person will approve the
download and publication of a new snapshot. Downstream projects will continue
to choose when to adopt a new snapshot.

Existing files in S3 will be preserved where practical. We may improve their
metadata or organization if the new implementation requires it, but we should
not redownload or duplicate large files without a clear reason.

## Ownership Boundaries

### IFX_Registry

IFX_Registry will own:

- checking whether upstream sources have changed;
- downloading and validating source files;
- recording source versions, dates, checksums, and provenance;
- uploading and retrieving immutable versions from AWS S3;
- maintaining the shared catalog of sources and artifacts;
- providing local cached copies to downstream projects; and
- hosting the Registry catalog and status pages currently in QA Browser; and
- building and registering declared, reusable derived datasets from exact
  registered inputs.

### IFX_harmonizers

IFX_harmonizers will continue to own:

- source-specific parsing and transformation;
- identifier harmonization and stable IFX identifiers;
- quality-control workflows, review decisions, and overrides; and
- versioned target, disease, drug, and variant releases.

Each Harmonizer domain release will be published to IFX_Registry as a derived
artifact. A target release, for example, will contain the related gene,
transcript, protein, and mapping files under one versioned manifest.

### IFX_ODIN

IFX_ODIN will continue to own:

- graph ETL and application database builds;
- adapters, graph models, and identifier resolvers;
- selection of exact Registry versions for a build; and
- graph-specific provenance and lineage views.

ODIN will consume Harmonizer release artifacts directly from IFX_Registry. The
current scripts that copy, stage, register, and repoint target files can then be
removed.

## Web Application Separation

The general Registry pages will move out of IFX_ODIN's QA Browser and into the
IFX_Registry web application. These include source and artifact catalogs,
manifest details, file information, and update status.

The shared S3 bucket is the sole source of truth for these pages. Server-local
downloads and manifests are transient staging for an acquisition job; they are
not Registry entries and are never presented to users as available datasets.
SQLite records operational job history only. If the bucket cannot be read, the
catalog reports that the Registry is unavailable rather than falling back to
local files.

ODIN will retain views that are specific to its graphs, such as which Registry
artifacts were used in a particular graph build. Those views will link to the
corresponding artifact in the Registry application.

## Delivery Plan

### 1. Establish the shared Registry

- Create the standalone Python package, command-line tool, web application,
  documentation, and tests.
- Move and simplify the existing Registry implementation from ODIN.
- Remove dependencies from Registry back into ODIN.
- Validate the new package against the data already stored in S3.

### 2. Migrate ODIN

- Replace ODIN's internal Registry code with the shared package.
- Move the Registry web pages to the new application.
- Preserve Registry provenance in ODIN graph metadata.
- Remove the duplicated implementation from ODIN after validation.

### 3. Migrate Harmonizer inputs

- Replace Harmonizer download and version-checking code incrementally.
- Start with sources shared by Harmonizers and ODIN, such as UniProt, ChEBI,
  Reactome, Rhea, WikiPathways, and major ontologies.
- Keep Harmonizer transformation and QC behavior unchanged during the move.

### 4. Publish Harmonizer outputs

- Publish one derived artifact for each Harmonizer domain release.
- Establish the target release as the first simplified Harmonizer-to-ODIN
  handoff.
- Follow with disease, drug, and variant releases.

### 5. Formalize package distribution

After the shared interface is stable, publish standard Python packages through
an organization-approved package index. Until then, each consumer will use an
explicit GitHub release tag rather than an unversioned branch.

## Key Decisions

- IFX_Registry is shared infrastructure, not part of ODIN's graph ETL.
- Registry stores versioned data, not resolver behavior or graph semantics.
- Source and derived snapshots share file storage and materialization; their
  kind remains explicit in APIs and provenance.
- Registry source acquisition and version checks have one implementation.
- Generic reusable derived-data recipes are owned and run by IFX_Registry;
  graph-specific transformations remain in IFX_ODIN.
- The shared S3 bucket is the Registry's authoritative catalog and artifact
  store; server-local files are disposable staging only.
- Consumer projects own their exact version selections; Registry does not
  advance them automatically.
- Harmonizer domain releases are multi-file derived artifacts.
- Harmonizer QC registries remain in IFX_harmonizers; they are not part of the
  data Registry.
- New Registry snapshots require human approval initially.
- IFX_ODIN is the first package consumer migration.
- The Registry catalog UI moves out of QA Browser; ODIN graph lineage stays.
- The package uses modern Python and supports Python 3.11 and newer.
- Old caller interfaces will be removed rather than maintained indefinitely.
- Legacy RaMP integration is out of scope. Its temporary cache implementation
  can remain until that build is retired.
- The initial deployment reuses `aws-ifx-registry` and its existing
  `sources/{source}/{dataset}/{version}` layout.
- The initial team deployment uses an ignored, read-only YAML credential file
  with the existing IFX assume-role fields. The adapter also supports the
  standard AWS credential chain so deployment can later move to a server IAM
  role without changing application code.
- A source version becomes visible only when `manifest.yaml` is created last
  with a conditional no-overwrite request.

## Risks and Controls

| Risk | Control |
| --- | --- |
| A shared change disrupts several projects | Use versioned Registry releases and upgrade each consumer deliberately. |
| Existing S3 data is incompatible | Inventory it first and migrate metadata only where necessary. |
| Harmonizer refactoring changes scientific results | Separate download changes from transformation changes and compare outputs. |
| Private GitHub installation complicates CI | Use an approved repository read identity, then move to a package index. |
| Registry grows into another ETL platform | Keep domain transformation, harmonization, and graph logic in their owning repositories. |
| Migration leaves two implementations active | Remove old code after each consumer passes its cutover checks. |

## Measures of Success

The separation is successful when:

- ODIN and Harmonizers use the same Registry package;
- source version checking and downloading are no longer duplicated;
- builds use exact, reproducible Registry versions;
- Harmonizer target releases flow directly to ODIN as versioned artifacts;
- the Registry catalog runs independently of QA Browser;
- ODIN retains clear graph provenance without owning Registry infrastructure;
  and
- the old Registry implementations and handoff scripts have been removed.
