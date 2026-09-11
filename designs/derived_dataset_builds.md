# Registry-Managed Derived Datasets

## Decision

IFX_Registry owns derived datasets that are generically useful to more than one
consumer. A derived dataset is a reusable, immutable data product built from
exact versions already registered in the catalog.

IFX_ODIN continues to own graph construction, graph-specific exports, resolvers,
and application database builds. IFX_Harmonizers continues to own domain
harmonization and scientific review workflows. Either project may still register
its reusable outputs in the Registry.

## User Experience

On a derived dataset page, a user can:

1. See the complete roots-to-leaves dependency tree.
2. See whether a newer registered input makes the latest output stale.
3. Select one exact registered version for every declared input.
4. Review the automatic output version and recipe revision.
5. Start a build and follow its progress.
6. Use the newly registered output after validation succeeds.

Installed recipes are also shown before their first output exists. Long-running
and failed builds remain visible in the Registry activity panel, including their
exact selected inputs and recipe revision. Exact-version pages remain immutable
provenance views; new builds start only from the dataset-level page.

For Registry-managed recipes, the output version is computed from the exact,
kind-qualified inputs and their manifest checksums, named recipe slots, producer
identity, effective transform, and recipe revision. The browser refreshes this
read-only `deps-…` value whenever an input changes, and the server independently
recomputes it before queueing and again before publication. Callers cannot
override it. Manually published derived datasets may continue to use meaningful
caller-supplied versions.

The catalog only shows a quiet warning when rebuilding is recommended. Exact
inputs and build controls remain on the details page.

The dependency tree groups exact versions under one dataset card. Solid lines
represent immutable snapshot dependencies. A live service queried during a
build is shown separately as subordinate, non-versioned build evidence; it does
not participate in freshness or rebuild calculations.

## Clean Architecture Boundary

The Registry domain contains immutable snapshots, exact input references, and
the declarative identity of an installed recipe. Materialized paths, recipe
products, and durable job records are application-layer models. Build execution
is added through application ports rather than embedded in web routes:

- a recipe catalog describes available reusable transformations and legal input
  slots;
- a build-job store records operational state;
- a workspace materializes exact registered inputs and removes temporary files
  after completion;
- a recipe runner produces files and validation results; and
- the existing derived publisher verifies inputs and registers the result.

Recipes may also return typed service observations for live APIs. The
application layer serializes these under immutable snapshot metadata. A live
observation is deliberately not registered as an `ExternalDatasetVersion`,
because it does not identify an independently addressable provider-issued
version.

The first implementation runs jobs through a single-process background worker,
as source acquisitions do today. Queue delivery is claimed atomically, so a
duplicate delivery is harmless. A future multi-process worker would add leases
and heartbeats behind the job-store port without changing recipe or UI
contracts.

Browser requests select only declared recipes, exact input versions, and typed
parameters. They never supply module paths, Python code, or shell commands.

## Rebuild Status

Rebuild status is calculated from the authoritative S3 catalog and is not
persisted as artifact metadata:

- **Current:** every input in the latest build is the latest registered version.
- **Rebuild recommended:** a direct or transitive input has a newer registered
  version.
- **Dependency missing:** an exact pinned input is absent.
- **Status unavailable:** lineage cannot be assessed safely, including cycles.

“Newer” means latest registered according to the Registry catalog. It does not
claim that an upstream provider has no newer release.

## Recipe Ownership

Reusable PubChem and SureChEMBL preparations have moved from IFX_ODIN's legacy
Registry package. The reusable ChEBI preparation remains to migrate after its
inputs, outputs, validation, and tests are made independent of ODIN types.

The PubChem compound-record client owns provider-specific traffic policy. It
batches 100 CIDs, leaves at least 0.25 seconds between every request, slows down
for the worst state in `X-Throttling-Control`, honors `Retry-After`, and retries
transient failures at most five times within a five-minute per-batch budget.
Only a definitive batch-level not-found response permits paced per-CID recovery;
transient exhaustion fails the build without request amplification. A successful
HTTP response is accepted only when its returned CIDs exactly match the request.

Graph-specific transformations remain in IFX_ODIN. Harmonizer release builds
remain in IFX_Harmonizers, while their completed releases are registered as
ordinary derived datasets.

## Delivery Sequence

1. Complete lineage browsing and rebuild-status assessment.
2. Add the generic recipe, build request, job, workspace, and runner contracts.
3. Add exact input selectors and build progress to derived dataset details.
4. Migrate reusable recipes incrementally and validate the full workflow.
   **Implemented:** the PubChem CID-set → compound-records → molecular-info
   chain and SureChEMBL protein patent-family mentions use the Registry runner.
5. Migrate the remaining reusable ChEBI recipe without changing its scientific
   result.
6. Remove the migrated implementations from IFX_ODIN after its callers use the
   Registry versions.
