# IFX Registry Development Guide

## Purpose

IFX_Registry owns the lifecycle of shared, versioned data artifacts. It checks
upstream versions, fetches source snapshots, validates and publishes immutable
artifacts, materializes pinned datasets, and presents the Registry catalog.

It does not own biomedical transformation, identifier harmonization, graph
construction, or consumer release policy.

## Architecture Rules

Dependencies point inward:

```text
presentation ----\
                  > application -> domain
infrastructure --/
```

- `domain` contains business concepts and errors. It imports only the Python
  standard library.
- `application` contains use cases and ports. It may import `domain`, but not
  infrastructure or presentation code.
- `infrastructure` implements application ports for HTTP sources, S3, local
  caches, configuration, and persistence.
- `presentation` exposes CLI and web entry points. It contains no registry
  business rules.
- A presentation-layer composition root may construct infrastructure adapters
  and inject them into application use cases; inner layers never perform that
  wiring.
- Source-specific code implements application ports; application code never
  switches on source names.
- The YAML source catalog may enable only allowlisted adapter keys. Do not load
  arbitrary module or class names from configuration.
- HTTP file-set sources extend the shared acquisition template and reuse a
  version strategy; do not duplicate pin guards, staging, progress, release
  rechecks, or atomic commit logic in individual sources.
- Prefer small, explicit interfaces. Do not require manual sources to implement
  automatic version discovery.
- Keep domain and application types independent of IFX_ODIN and
  IFX_harmonizers.
- Use exact dataset versions in reproducible consumer configurations.
- Published artifacts are immutable.
- The shared S3 bucket is the Registry's sole authoritative artifact catalog.
  Server-local downloads and manifests are staging only and must never appear
  as registered or available datasets.
- Publish artifact files before the canonical manifest and create that manifest
  with no-overwrite semantics. A version becomes visible only at the manifest
  commit point.
- Catalog reads come from S3. A local cache may improve performance but must not
  masquerade as authoritative data when S3 is unavailable.

## Quality Rules

- Target Python 3.11 or newer.
- Add unit tests for domain behavior and application use cases.
- Add contract tests for every infrastructure adapter.
- Keep network and filesystem side effects behind ports.
- Use dependency injection instead of module-level clients or hidden globals.
- Raise domain-specific exceptions with actionable context.
- Do not add compatibility code for legacy RaMP.
