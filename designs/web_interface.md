# IFX Registry Web Interface

## Purpose

The Registry website helps developers discover which shared datasets are
available, inspect their exact immutable versions, and understand how to use
them. On a managed dataset page, an operator can also understand its upstream
provenance, check for a different release, and register that exact release.

The application runs on a server for the IFX team. The shared AWS S3 bucket is
the Registry and its sole source of truth. Server-local files are temporary
implementation details and do not appear in the catalog.

## Primary Developer Workflow

The home page is a compact dataset catalog. It answers these questions without
requiring knowledge of the Registry implementation:

1. What datasets are available?
2. Which exact versions have been registered?
3. When was each version released and registered?
4. Which files and validation evidence belong to a version?
5. Which pinned identifier or supported command should a caller use?

The primary journey is:

```text
Dataset catalog
  -> dataset and registered versions
  -> exact version, files, validation, provenance, and usage
```

The catalog shows only versions whose canonical manifests are present in S3.
An incomplete upload, local staging directory, or merely validated download is
not an available Registry version.

## Operator Workflow

Source maintenance is available from the main catalog and in more detail on
each managed dataset page. There is no separate operations destination. For
each enabled source, an operator can:

1. Check the version currently reported by the upstream provider.
2. Review the exact version proposed for registration.
3. Approve a **Register release** operation.
4. Follow the job through downloading, validation, publication, and completion.
5. Open the registered version in the developer catalog.

The initial workflow treats approval of **Register release** as approval to
commit the version after successful validation. There is no useful user-facing local
snapshot between validation and publication. If the team later needs a second
human approval, it can be added as an explicit release-policy step rather than
as a consequence of local storage.

The service prevents duplicate active work and immutable-version replacement.
It checks the upstream version before and after transfer so a changing release
cannot be published under the wrong identifier.

Successful upstream checks are operational observations stored in SQLite, not
Registry artifacts. The catalog reuses a check for seven days by default and
shows whether that observed version matches authoritative S3 state. After the
window expires, the UI recommends another check without claiming the registered
dataset itself is stale. Failed checks do not erase the last successful
observation. The window is configurable with
`IFX_REGISTRY_VERSION_CHECK_TTL_SECONDS`.

## Information Architecture and Content

The catalog begins with a small heading, a plain description, search, and the
dataset list. It does not use a promotional hero or explain migration history,
implementation boundaries, future rollout plans, server paths, or other team
inside knowledge.

A decorative card-catalog image may appear as one non-tiled, compact title
band on the catalog page. A contrast overlay keeps its printed drawer labels
atmospheric rather than presenting them as real Registry categories. The image
does not appear in operations or behind functional controls.

Dataset rows use the recognizable data-source name—such as Antibodypedia or
BioPlex—as the primary title. The dataset name and stable ID remain immediately
below it as the more technical distinction. Rows also show the latest registered
version, relevant dates, file count, total size when available, and a detail
action. Dataset details list every registered version. Exact-version details
show the pinned identifier, usable artifact references, files, sizes,
checksums, provenance, and concrete validation evidence.

Milestone UI reviews render the real catalog at desktop and phone widths. On
narrow screens, dataset and file tables become stacked semantic rows so actions,
long versions, checksums, and storage paths remain inside the viewport.

A dataset page shows the source website, acquisition endpoints, and a plain-
language explanation of its version check. Technical endpoint details use
progressive disclosure. A compact **Update Registry** panel keeps the latest
registered version visible, distinguishes checking from registration, and only
offers **Download and register _version_** after a successful check reports a
different exact version. It never assumes heterogeneous version strings can be
ordered. Active work disables duplicate acquisition.

Registered datasets without an installed source adapter remain visible and
explicitly read-only. Configured sources with no registered version appear in a
separate secondary section rather than being mixed into the authoritative S3
catalog. Active registrations remain visible in a compact sticky right-hand
activity rail while the user browses. The catalog clearly distinguishes an empty
Registry from access denied, an unreachable bucket, or inconsistent catalog
metadata.

## Source Configuration

A strict YAML catalog selects the source adapters that operators may manage and
controls their display names and descriptions. Adapter keys are resolved
through a code-owned allowlist; configuration cannot import arbitrary classes.
Source adapters expose their homepage and acquisition endpoints, and reusable
version strategies provide both their evidence endpoints and a plain-language
explanation. This keeps the UI accurate as source behavior evolves. Source code
owns URLs, version detection, file sets, and validation rules.

YAML describes installed source capabilities. It does not determine which
artifacts are available. Published S3 manifests determine catalog availability.

## Application Shape

The interface is a standalone FastAPI application with server-rendered Jinja
templates, plain CSS, and a small amount of JavaScript. It has no dependency on
IFX_ODIN and no frontend build system.

```text
Browser
  -> catalog and contextual dataset update routes
  -> browse/register application use cases
  -> installed-source, job, staging, and published-snapshot ports
  -> source adapters, SQLite job history, temporary server workspace, and S3
```

The application layer owns the publication workflow. Infrastructure adapters
own HTTP, local staging, SQLite, and S3 details. Presentation code does not
query providers, manipulate files, run SQL, or construct S3 keys directly.

## Publication Boundary

A registration job follows this sequence:

```text
check -> stage download -> validate and checksum -> upload files
      -> publish canonical manifest last -> verify -> succeed
```

Publishing the canonical manifest is the commit point. Uploaded files without
that manifest remain invisible to the catalog and can be cleaned up or retried.
Creating the manifest uses no-overwrite semantics so a published version is
immutable.

Job success means the version is published and readable from S3. A validated
staging directory is not success. The service may cache catalog data for
performance, but a cache never substitutes for S3 when S3 is unavailable.

## Operational State and Deployment

Docker Compose remains the default deployment path. The initial deployment
mounts the team's ignored AWS assume-role YAML file read-only. Credentials are
not entered in the web page or stored in artifact manifests. The adapter also
accepts the standard AWS runtime identity for a later deployment change.
The service is deployed on the private team network behind authenticated
reverse-proxy access, and write requests enforce a same-origin check. It must
not be published directly to the internet with Registry credentials attached.

SQLite may retain shared job and activity history while the service runs as one
replica. Temporary job workspaces are removed after publication or failure.
Neither is part of the Registry catalog.

## Implemented First Release

The first vertical slice now provides:

1. An S3-backed published-snapshot catalog and publisher behind application
   ports.
2. File-first publication with an immutable canonical manifest as the commit
   point.
3. A searchable catalog page and exact-version pages populated only from S3.
4. Contextual update controls on the catalog and dataset pages, with active
   acquisition jobs held in a sticky activity rail.
5. Compatibility with the existing `sources/{source}/{dataset}/{version}`
   layout and schema-version-1 manifests.
6. Tests for commit order, idempotent retry, immutable conflicts, canonical
   object locations, catalog consistency, and the complete web workflow.

Pinned snapshot materialization for Python callers is implemented. Registering
derived Harmonizer artifacts remains a separate future increment.
