# IFX Registry

IFX Registry is the shared source and artifact registry for IFX projects. It
provides reproducible access to pinned biomedical source and derived datasets,
plus sanitized version records for external systems.

The initial supported consumers and producers are IFX_ODIN and
IFX_harmonizers. Legacy RaMP integration is intentionally out of scope.

## Install the Client

Until the package moves to an approved package index, pin an immutable Git tag
in the consuming project's `requirements.txt`:

```text
ifx-registry @ git+https://github.com/ncats/IFX_Registry.git@v0.2.0
```

Then install that project's requirements normally. The default package contains
only the S3 client and its YAML credential support. Developers running the
Registry service itself install the server extra instead:

```bash
python -m pip install ".[server]"
```

## Use a Dataset

Connect with the team's credential file, then ask for one exact registered
version:

```python
from ifx_registry import RegistryClient

registry = RegistryClient.connect("aws_ifx_registry.yaml")
path = registry.file("reactome:pathways:97", "ReactomePathways.txt")
```

Pass no credential file when the standard AWS credential chain or a server IAM
role already supplies access:

```python
registry = RegistryClient.connect()
```

The team credential YAML uses the existing IFX assume-role shape:

```yaml
type: aws_assume_role
access_key_id: YOUR_ACCESS_KEY
secret_access_key: YOUR_SECRET_KEY
role_arn: arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME
bucket: aws-ifx-registry
region: us-east-1
```

Keep that file outside version control.

For a dataset containing one file, the file name is optional:

```python
path = registry.file("hcop:human_all_sixteen_column:2026-09-09")
```

For access to a dataset's metadata or several named files:

```python
dataset = registry.materialize("reactome:pathways:97")
hierarchy = dataset.file("ReactomePathwaysRelation.txt")
```

Inspect filenames, sizes, checksums, and provenance without downloading files:

```python
description = registry.describe("reactome:pathways:97")
print(description.file_names)
print(description.total_size_bytes)
```

Consumer-facing errors are available from the package root. Callers can catch
specific recovery cases or use `RegistryError` as the common base:

```python
from ifx_registry import RegistryUnavailableError, SnapshotNotFoundError

try:
    path = registry.file("reactome:pathways:97", "ReactomePathways.txt")
except SnapshotNotFoundError:
    raise RuntimeError("The pinned Registry version has not been registered")
except RegistryUnavailableError:
    raise RuntimeError("The Registry is temporarily unavailable")
```

The client manages locking, downloads, and checksum verification. It uses
`/var/tmp/ifx-registry-cache` by default; set `IFX_REGISTRY_CACHE_DIR` or pass
`cache_dir=` when connecting to change the cache root. Files are stored below
that root as `source/dataset/version/...`; derived files use
`derived/source/dataset/version/...` to prevent collisions. The cache is
persistent, shared by processes on the same machine, and intentionally
unbounded. Every access hashes
the selected local file; a missing or mismatched file is downloaded to a
temporary path, verified, and atomically replaced. Operators may delete unused
version directories when no consumer is running because S3 remains
authoritative. The caller must provide a pinned `source:dataset:version` ID—
there is no implicit `latest` substitution.

The root `file()`, `files()`, `materialize()`, and `describe()` methods consume
registered source snapshots only. Derived datasets use the explicit
`registry.derived` namespace; the root methods never guess an artifact kind,
build recipes, or resolve `latest`.

## Register a Caller-Supplied Source Dataset

For a manual download or provider export, the caller chooses the exact Registry
paths and records how the files were obtained:

```python
from datetime import UTC, datetime

published = registry.publish_source(
    "antibodypedia:export:2026-09-10",
    files={"antibodypedia.tsv": downloaded_file},
    captured_at=datetime.now(UTC),
    capture_method="provider_export",
    version_evidence={"provider_release": "2026-09-10"},
    validation={"rows": 12_345},
)
```

Registry reads, hashes, and uploads the files but never moves or deletes the
caller's copies. Repeating the exact publication is safe; changing files or
metadata under the same pinned ID is rejected.

## Build or Register Derived Data

IFX Registry owns reusable transformations that produce generally useful data
products. An installed recipe declares its legal inputs, lets an operator select
exact registered versions in the web application, runs in a disposable
workspace, validates the result, and registers the new immutable version. The
installed recipes build:

- `pubchem:compound_cid_set` from exact HMDB, WikiPathways, LIPID MAPS, and
  RefMet versions;
- `pubchem:compound_records` from an exact CID-set version plus live, recorded
  PubChem PUG REST observations;
- `pubchem:cid_molecular_info` from exact compound records;
- `ncbi:human_gene_identifier_mappings` from an exact NCBI Gene identifier
  mapping snapshot, filtered to human records;
- `ensembl:uniprot_isoform_xrefs` from exact Ensembl BioMart and UniProt
  release inputs;
- `uniprot:uniref100_memberships` from an exact pre-UniRef target protein-ID
  handoff plus a matching UniProt release; and
- `surechembl:protein_patent_family_mentions` from an exact SureChEMBL patent
  discovery snapshot.

The built-in Targets source catalog also includes focused human snapshots for
the UniProt reference proteome, UniProt ID mappings, UniProt isoforms, and the
Babel Gene and Protein compendia. The Babel acquisitions stream upstream
chunks through temporary storage and retain only human records, avoiding a
second permanent copy of the very large global compendia.

The reusable ChEBI preparation is the remaining legacy recipe to migrate.

PubChem requests are sent in batches of 100 and are paced at no more than four
requests per second. Transient network, HTTP 429, HTTP 5xx, invalid JSON, and
incomplete-response failures receive bounded exponential retries that honor
`Retry-After` and PubChem's dynamic throttle header. Exhausted transient batches
fail closed and are never expanded into 100 individual requests. Every
successful build records immutable request, retry, status, throttle, observation
window, endpoint-template, and stored-response digest evidence in its manifest.

Projects may also produce specialized derived data outside the Registry. For
example, IFX_Harmonizers owns scientific harmonization workflows and can hand
their completed, reusable releases to Registry for immutable storage. In that
case Registry verifies every exact input pin, records the provenance, and
registers the result:

```python
from ifx_registry import ProducerIdentity, SnapshotRef

published = registry.derived.publish(
    "ifx_harmonizers:targets:2.2.0",
    files={
        "gene_ids.tsv": gene_ids,
        "protein_ids.tsv": protein_ids,
        "uniprot_mapping.csv": uniprot_mapping,
    },
    inputs=[
        SnapshotRef.source("hgnc:complete_set:2026-09-01"),
        SnapshotRef.source("uniprot:human:2026_03"),
    ],
    producer=ProducerIdentity(
        name="ifx_harmonizers",
        release="2.2.0",
        code_repository="https://github.com/ncats/IFX_harmonizers",
        code_revision=git_commit_sha,
    ),
    transform={"name": "target_harmonization", "version": "2"},
    validation={"protein_count": 20_386, "unmapped_count": 14},
)
```

The `files` mapping explicitly chooses each path inside the registered dataset.
Registry reads but never moves or deletes the caller's files. Input kind is
also explicit, so a derived dependency uses
`SnapshotRef.derived("producer:dataset:version")`.
An external system version may be an equally explicit input with
`SnapshotRef.external("chembl:activity_database:chembl36")`.

Consumers use the matching namespace:

```python
description = registry.derived.describe("ifx_harmonizers:targets:2.2.0")
protein_ids = registry.derived.file(
    "ifx_harmonizers:targets:2.2.0",
    "protein_ids.tsv",
)
dataset = registry.derived.materialize("ifx_harmonizers:targets:2.2.0")
```

Repeating a publication with identical files, inputs, producer identity,
transformation, validation, and metadata returns the registered description.
Any difference under the same ID raises `SnapshotAlreadyExistsError`. Files are
uploaded first and the manifest is committed last, so incomplete publication
attempts never appear in the catalog.

## Register an External Dataset Version

External registrations describe a version that Registry does not store, such
as a database schema or service release:

```python
registered = registry.external.register(
    "chembl:activity_database:chembl36",
    interface="mysql",
    access_mode="query",
    service_name="ChEMBL activity database",
    observed_at=datetime.now(UTC),
    documentation_url="https://www.ebi.ac.uk/chembl/",
    version_check={"type": "schema_release"},
    version_evidence={"release": "chembl36"},
)

description = registry.external.describe("chembl:activity_database:chembl36")
versions = registry.external.list(source="chembl")
```

These records contain descriptive, non-secret metadata only. Registry rejects
credential-bearing fields and does not provide `file()` or `materialize()` for
external versions; the consuming application owns the live connection.

## Architecture

The repository follows Clean Architecture. Business rules do not depend on
AWS, HTTP libraries, web frameworks, command-line frameworks, or consumer
repositories.

```text
src/ifx_registry/
├── domain/              # Registry concepts and business invariants
├── application/
│   ├── ports/           # Interfaces implemented by external adapters
│   └── use_cases/       # Registry workflows
├── infrastructure/
│   ├── sources/         # Shared acquisition strategies and source declarations
│   └── source_configuration.py # Strict installed-source configuration
├── config/              # Default source catalog
└── presentation/
    ├── cli/             # Command-line delivery
    └── web/             # Web delivery
```

Dependencies always point toward the domain:

```text
presentation ----\
                  > application -> domain
infrastructure --/
```

See [the architecture design](designs/ifx_registry_architecture.md) for the
cross-repository ownership and migration plan.

## Installed Source Configuration

The web application reads its source list from
`src/ifx_registry/config/sources.yaml`. Configuration decides which known
adapters are enabled and supplies their user-facing names and descriptions:

```yaml
schema_version: 1
sources:
  - adapter: reactome_pathways
    enabled: true
    display_name: Reactome Pathways
    description: Pathway definitions, mappings, and interactions.
```

`adapter` is an allowlisted key, not a Python import path. URLs, version rules,
file declarations, and validation remain in tested adapter code. Unknown
fields, invalid types, duplicate entries, and unknown adapters stop application
startup with an actionable configuration error.

Use another catalog locally with `ifx-registry-web --sources sources.yaml`, or
set `IFX_REGISTRY_SOURCES_CONFIG` in a deployment. Adding a genuinely new
adapter requires a small, reviewed factory registration before YAML may enable
it.

## Implementing a Source

An automatically refreshable source implements `SourceAdapter`, which combines
two small ports:

- `SourceVersionProbe` discovers the latest upstream version.
- `SourceFetcher` downloads one source snapshot.

```python
from datetime import timedelta
from pathlib import Path

from ifx_registry import (
    DatasetId,
    FetchRequest,
    SourceAdapter,
    SourceSnapshot,
    SourceVersion,
    VersionProbeRequest,
)


class ExampleSource(SourceAdapter):
    @property
    def dataset(self) -> DatasetId:
        return DatasetId(source="example", dataset="records")

    @property
    def homepage(self) -> str:
        return "https://example.org/records"

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return ("https://example.org/records.tsv",)

    @property
    def version_check_description(self) -> str:
        return "Reads the published release identifier from the metadata endpoint."

    @property
    def version_evidence_urls(self) -> tuple[str, ...]:
        return ("https://example.org/version",)

    def discover_latest(self, request: VersionProbeRequest) -> SourceVersion:
        # Query upstream release metadata using request.timeout.
        return SourceVersion(value="2026-09-08")

    def fetch(self, request: FetchRequest) -> SourceSnapshot:
        # Download into request.destination and describe the resulting files.
        raise NotImplementedError
```

Manual or restricted sources implement only `SourceFetcher`. They are not
forced to claim that automatic version discovery is available.

For the common case of one versioned HTTP file set, extend
`HttpSnapshotSource` and declare:

- a `DatasetId`, homepage, and ordered `HttpFileSpec` collection;
- a reusable version strategy, such as `TextEndpointVersionStrategy` or
  `HeaderVersionStrategy`; and
- only the source-specific validation hook.

The shared template performs the initial version check, expected-version guard,
staged downloads, progress reporting, validation, final version recheck, and
atomic commit. Source adapters should not reimplement that sequence.

Source adapters return domain objects. They do not create manifests, upload to
S3, choose versions for consumers, or perform downstream biomedical
transformation. Application use cases orchestrate those responsibilities.

## Built-in Sources

The initial specialized adapters exercise different upstream patterns:

- `ReactomePathwaysSource` obtains Reactome's database release number and
  downloads the five pathway, mapping, and interaction files as one snapshot.
- `UniProtHumanSource` reads UniProt release headers, streams the complete and
  reviewed human JSON files, and verifies that every reviewed accession occurs
  in the complete file.

They use the same injected HTTP gateway and transactional snapshot workspace.
A failed download or validation never exposes a partial version directory, and
an existing version directory is never replaced. Each adapter also checks the
upstream release again before publishing its local snapshot, so a release that
changes during a multi-file download is rejected rather than mislabeled.

```python
from pathlib import Path

from ifx_registry import FetchRequest, FetchSource, VersionProbeRequest
from ifx_registry.infrastructure.http import RequestsHttpGateway
from ifx_registry.infrastructure.sources import UniProtHumanSource

http = RequestsHttpGateway()
source = UniProtHumanSource(http)
version = source.discover_latest(VersionProbeRequest())

snapshot = FetchSource().execute(
    source,
    FetchRequest(
        destination=Path("registry-cache"),
        expected_version=version,
    ),
)

print(snapshot.snapshot_id)
for file in snapshot.files:
    print(file.local_path)
```

These source adapters acquire the current upstream release. If the upstream
has moved since the caller pinned `expected_version`, acquisition stops before
any large files are downloaded. Resolving older pinned versions from published
Registry storage belongs to the later materialization use case.

The acquisition workflow publishes validated files to the shared S3 bucket and
then creates the canonical manifest with no-overwrite semantics. That manifest
is the commit point: the web catalog shows only committed S3 snapshots, never
temporary server files or incomplete uploads.

## Development

Create a Python 3.11 or newer environment, then install the development tools:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run the initial quality checks:

```bash
pytest
ruff check .
mypy src
```

## Run the Web Application

The normal deployment path is Docker Compose:

```bash
docker compose up --build
```

The web application does not implement team identity itself. Deploy it only on
the private team network and behind the team's authenticated reverse proxy; do
not expose port 8000 directly to the public internet. Unsafe web requests also
require a same-origin browser header to prevent cross-site form submissions.

Open <http://localhost:8000>. The service reads and publishes the existing
`aws-ifx-registry` bucket by default. SQLite job history is retained in the
`registry-state` named volume; temporary downloads are removed when each job
finishes. To use a different host port or bucket:

```bash
IFX_REGISTRY_WEB_PORT=8080 docker compose up --build
IFX_REGISTRY_BUCKET=my-registry docker compose up --build
```

Successful upstream version checks are retained in SQLite for seven days, so a
page reload reuses a recent “Up to date” or “Version available” result instead
of querying the provider again. Override the window when needed:

```bash
IFX_REGISTRY_VERSION_CHECK_TTL_SECONDS=604800 docker compose up --build
```

Registry timestamps are stored in UTC and displayed in US Eastern time by default.
Set another IANA timezone for the web interface when needed:

```bash
IFX_REGISTRY_DISPLAY_TIMEZONE=America/Chicago docker compose up --build
```

When the reverse proxy publishes the application beneath a URL prefix, configure
that ASGI root path so generated links, static assets, form actions, and redirects
retain the prefix:

```bash
IFX_REGISTRY_ROOT_PATH=/registry docker compose up --build
```

The Compose deployment trusts reverse-proxy forwarding headers so FastAPI sees
the browser-facing scheme and client address across Docker's bridge network.
Keep the published Registry port behind the trusted reverse proxy, as required
above; do not expose it directly to untrusted clients.

This first implementation uses a local SQLite job database and a serialized
background download worker, so deploy it as one application replica. Docker
Compose reads `secrets/aws_ifx_registry.yaml`, using the same
`aws_assume_role` shape as IFX_ODIN. Copy the team credential file there before
starting the service; the directory is ignored by Git, the file is mounted
read-only, and credentials are never entered through the web page or stored in
artifact manifests.

For local Python development without containers:

```bash
ifx-registry-web --state-dir registry-state
```

For a native local run, pass the YAML path explicitly:

```bash
ifx-registry-web --aws-credentials secrets/aws_ifx_registry.yaml
```

The standard AWS credential chain remains available when no YAML file is
configured, which allows a future server IAM role without an application
change.

Optionally select another installed-source catalog:

```bash
ifx-registry-web --state-dir registry-state --sources sources.yaml
```
