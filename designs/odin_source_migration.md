# IFX_ODIN Source Migration

## Goal

Move reusable version checking, downloading, validation, and registration out
of IFX_ODIN and into IFX_Registry while keeping every ODIN build pinned and
reproducible. S3 remains the authoritative Registry. ODIN keeps only a local,
checksum-verified materialization cache for files needed by a build.

This migration explicitly excludes Antibodypedia, legacy RaMP support, the
IFX_Harmonizers repositories and derived-artifact work, and Jess's local
`impatient_target_graph` reconstruction.

## What a Build Will Do

An ODIN YAML file continues to name an exact dataset version, for example:

```yaml
data_source: reactome:pathways:97
```

At configuration load time, ODIN asks IFX_Registry to materialize that exact
version. IFX_Registry reads the canonical S3 manifest, reuses a local file only
when its size and SHA-256 match, otherwise downloads through a temporary file,
verifies it, and atomically installs it in the configured cache. The resulting
`MaterializedDataset` supplies named files and manifest version metadata to the
existing input adapter.

The Registry service's acquisition workspaces remain temporary and are removed
after success or failure. An ODIN materialization cache is a separate consumer
cache. It will be configurable so operators can place it on suitable storage;
shared use requires per-artifact locking and atomic writes.

## Discovery Summary

ODIN already has the correct consumer seam in `src/core/config.py`:
`data_source` and related YAML fields are resolved before adapters are built.
The current `DataRegistry.materialize_source_snapshot()` also demonstrates the
required compatibility behavior. The new package should replace this source-
snapshot path without changing adapter constructor arguments.

The production Pharos and target-graph YAML files already pin Registry IDs for
the majority of source inputs. The working metabolite workflow also uses pinned
source IDs. Pounce still has direct Snakemake downloads for Cellosaurus and
Ensembl; UniProt is already represented in the Registry. External databases,
resolver snapshots, and derived artifacts use different manifest kinds and
must continue through ODIN's existing implementation during this source-only
increment.

IFX_Registry currently requires Python 3.11 or newer. ODIN's checked-in local
virtual environment is Python 3.11.14, so the pilot is compatible. This does not
reintroduce support for the short-lived Python 3.9 RaMP implementation.

## Implementation Order

### 1. Pinned source materialization

Implement three narrow package capabilities:

- get one canonical source manifest by `source:dataset:version`;
- download its declared files through temporary paths and verify size and
  SHA-256 before atomic rename;
- return a framework-independent `MaterializedDataset` with named-file access.

Compatibility requirements include schema-version-1 YAML manifests, paginated
S3 access, safe relative paths, and legacy manifests whose recorded S3 bucket
does not match the bucket containing the canonical manifest. Concurrent
materializers must not expose partial files.

### 2. ODIN consumer pilot

Install IFX_Registry from a pinned Git tag and adapt only
`_materialize_registry_data_source()` in `src/core/config.py`. Source snapshots
use the new package; derived, resolver, and external references retain the
existing ODIN `DataRegistry` path.

Pilot with a small `working.yaml` slice and then representative pinned sources
from the Pharos configuration. Dataset IDs, versions, filenames, adapter
arguments, and provenance values do not change. The user runs Snakemake and ETL
builds; automated tests exercise configuration resolution and cache behavior.

### 3. Acquisition cohorts

Migrate source acquisition in cohorts, proving each shared abstraction with two
different sources before expanding it.

1. **HTTP Last-Modified**: HCOP and MGI pilots; then IMPC, Uberon, GO ontology,
   GOA human datasets, MONDO, PubTator, NCBI gene summary, JensenLab tissues and
   protein counts. Extend the same helper to multi-file NCBI publications,
   JensenLab diseases, ExPASy, and IUPHAR. Keep JensenLab TINX custom because it
   also transforms/compresses output.
2. **Embedded metadata**: MP OBO `data-version`, Disease Ontology OWL metadata,
   and ChEBI release metadata cross-checked against its downloaded ontology.
3. **Provider release/listing**: WikiPathways GMT and RDF, PFOCR, TIGA, BioPlex,
   STRING, PANTHER, Pathway Commons, Rhea, HPA, GTEx, and SureChEMBL. Share
   listing mechanics only where two providers demonstrate the same invariant;
   do not create a generic HTML-regex strategy.
4. **Specialized automatic sources**: CTD, GlyGen, CURE case reports, LipidMaps,
   RefMet, and ChEBI three-star SDF. These use the common acquisition lifecycle
   but retain source-specific version or validation logic.
5. **Pounce gaps**: add Cellosaurus and Ensembl source adapters, then replace
   their direct Snakemake downloads with pinned Registry references. RaMP stays
   excluded.

Reactome and UniProt are already implemented and have completed real version
check, download, validation, and registration runs through the new interface.

## Implementation Status

The Registry now enables 42 automatically refreshable datasets and contains a
disabled adapter for one additional source. This covers every currently
accessible automatic source that was declared in IFX_ODIN's legacy Registry
source catalog except the deliberately excluded Antibodypedia scraper and
legacy RaMP database. The final migrated batch is:

- CURE ID case reports: timestamped, complete paginated API export adapter,
  disabled because the upstream now requires a data use and transfer agreement
  and credentials for external API access;
- GlyGen human proteins: upstream `listcache_id` version and CSV download;
- Dark Kinome: dated Registry capture of kinase links;
- RESOLUTE: dated Registry capture of SLC genes from GraphQL; and
- LinkedOmicsKB: dated Registry capture of its gene list.

These five adapters have automated contract coverage for version identity,
payload validation, stable output shape, and generated files. GlyGen, Dark
Kinome, RESOLUTE, and LinkedOmics have also completed live probe and temporary
fetch validation. CURE returned its provider's explicit access-agreement error
on September 9, 2026, so it is not presented as refreshable. S3 registration
and pinned ODIN materialization remain operational acceptance steps; no adapter
makes content authoritative until an S3 registration succeeds.

Cellosaurus and Ensembl remain the two direct-download gaps used by Pounce's
Snakemake workflow. They were not entries in the old Registry source catalog
and are the next optional cohort if Pounce will live long enough to justify the
work. Manual inputs, derived artifacts, and external database references retain
the separate contracts described below.

## Sources That Need Different Contracts

- Dark Kinome, RESOLUTE, and LinkedOmics have no trustworthy automatic release
  identifier. Treat them as explicitly requested dated captures rather than
  pretending to discover a latest release.
- HPM, HMDB, CURE curated concepts, and target-graph files are manual inputs.
  They require a manual registration interface, not `HttpSnapshotSource`.
- ChEBI's raw ontology/SDF downloads and SureChEMBL's raw patent-discovery
  download use source adapters in the cohorts above. Their transformed outputs
  (ChEBI endogenous metabolites and SureChEMBL patent-family mentions), along
  with PubChem and target/drug graph outputs, belong to a later producer
  contract. IFX_Harmonizers remains paused.
- RDAS, ChEMBL, DrugCentral, IFX PubMed, and legacy Pharos are external
  registrations rather than downloadable source snapshots.

## Cohort Acceptance Gate

For every migrated cohort:

1. Compare old and new version discovery against the same live response.
2. Verify the exact file inventory, stable filenames, version evidence,
   validation metadata, and failure behavior.
3. Complete a temporary live fetch that is removed after validation, then
   enable the source in the web catalog. A source that needs unavailable access
   credentials remains disabled.
4. From the web catalog, register only a genuinely unregistered version and
   confirm that existing S3 versions are recognized and never overwritten.
5. Materialize the registered version through the package and compare size and SHA-256 with its
   manifest.
6. Resolve the pinned ID through ODIN configuration and run the relevant narrow
   working build.
7. Remove its ODIN downloader only after the Registry path is proven. Do not
   automatically change pinned versions in production YAML.

## Validation Responsibility

Automated unit and contract tests do not require AWS. Live version probes are
read-only. Actual Snakemake and ETL executions remain user-run unless explicitly
delegated. Validation instructions will name the narrow workflow and expected
file/version comparisons for each cohort.
