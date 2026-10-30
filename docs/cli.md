# enkictl CLI

The Python CLI collects platform intent and calls Helm, Kustomize and kubectl
directly. It does not call the shell installers. Helm values and Kubernetes
manifests remain the source of truth; orchestration pins live in
[`helm/platform.yaml`](../helm/platform.yaml).
Component-specific composition, credentials and readiness live in
[`cli/enkictl/components/`](../cli/enkictl/components/), with optional addon
lifecycle commands under [`cli/enkictl/addons/`](../cli/enkictl/addons/);
ownership and execution remain shared. The superseded core shell installers
have been removed.

## Install the tooling

Use Python 3.12 or newer, Helm 3 or 4, and kubectl compatible with your server.
Standalone Kustomize is optional; kubectl's embedded Kustomize is the fallback.
Run from this checkout, or supply `--stack-root /path/to/enki.stack` on deployment
commands. The CLI neither provisions Kubernetes nor changes kubeconfig.

```bash
python -m pip install -e .
enkictl --help
```

If Hub's gitignored upstream assets are missing, prepare them using the existing
vendoring procedure in [the Hub guide](kubeflow-hub.md) before deployment.
Do not apply legacy whole-stack overlays alongside the CLI on the same installation.
Only fresh installations and subsequent CLI-owned updates are supported; legacy
resources are never silently adopted.
Inventory schema 2 records explicit component ownership. Prior CLI inventory
schemas are rejected, not converted or discarded. Use the original CLI to
operate an old installation and back up state before creating a fresh one.
In-place domain, GPU-management and certificate-mode/environment migrations are
not supported for affected installed components: uninstall those components and
their dependents first (retaining data), revise the profile, then
reinstall. Ordinary reruns reconcile the same topology and preserve credentials.
Changing unused domain/auth settings does not require removing an unrelated
standalone GPU Operator.

## Components and selection

```bash
enkictl component list -p profiles/customer/profile.yaml
enkictl addon list -p profiles/customer/profile.yaml
enkictl init -p profiles/customer/profile.yaml
enkictl init --component auth --component kserve -p profiles/customer/profile.yaml
enkictl install -p profiles/customer/profile.yaml --dry-run
enkictl install -p profiles/customer/profile.yaml
enkictl uninstall -p profiles/customer/profile.yaml --dry-run
enkictl validate auth kserve -p profiles/customer/profile.yaml --render
enkictl preflight auth kserve -p profiles/customer/profile.yaml
enkictl component install auth kserve -p profiles/customer/profile.yaml --dry-run
enkictl component install auth kserve -p profiles/customer/profile.yaml
enkictl component install monitoring -p profiles/customer/profile.yaml
enkictl component uninstall kserve -p profiles/customer/profile.yaml --dry-run
```

| Target | Includes | Required components |
| --- | --- | --- |
| `certificates` | cert-manager, platform issuance/export, conditional self-signed trust-manager/Bundle | None |
| `ai-gateway` | Envoy Gateway and AI Gateway controllers/CRDs plus the shared HTTPS Gateway | certificates |
| `auth` | Dex, connector, auth route and stable Grafana/Hub/LibreChat client identities | ai-gateway |
| `monitoring` | Prometheus/Grafana/Tempo, dashboards, scrapes, Grafana OAuth and conditional trust reloader | auth |
| `gpu-operator` | NVIDIA GPU Operator with the existing DGX OS driver | None |
| `kserve` | KServe, LLMInferenceService and runtime configs | certificates, ai-gateway |
| `kubeflow-hub` | Hub/MySQL/PostgreSQL/Zot, credentials and protected publication | auth |

These are supported stack configurations, not the minimum upstream product
dependencies. In particular, Envoy Gateway and AI Gateway deliberately remain
one target because their current extension-service configuration is coupled.

The KServe CRD charts both contain `clusterstoragecontainers.serving.kserve.io`.
The CLI keeps that CRD solely in the `kserve-crd` release, excluding it from
`kserve-llmisvc-crd` through the Helm post-renderer during previews and installs.
The LLM-specific CRDs are still installed. This avoids changing the shared CRD's
Helm release annotation or forcing field conflicts. If an earlier install failed
on that annotation conflict, rerun the same install command with the retained
inventory; do not delete the CRD or use `--force-conflicts`.

CRD installation polls for `Established=True` for up to 120 seconds, tolerating
missing or null conditions while the API server initializes status. This avoids
`kubectl wait` accessor errors on newly created CRDs. A timeout reports the CRD
name and its last conditions; API access errors are not retried or suppressed.

Ownership labels are added to resource metadata and supported workload pod
templates. KServe `LLMInferenceServiceConfig.spec.template` is a PodSpec, not
a pod template with metadata, and is left unchanged. If runtime-config
installation previously failed with `.spec.template.metadata: field not declared
in schema`, rerun the component install with the retained inventory after
updating the CLI.

The CLI also normalizes whole-core CPU quantities in LLM config containers
(`4000m` to `"4"` and numeric values to strings) to match controller serialization.
CPU amounts are unchanged; this avoids Helm server-side apply conflicts on
semantically identical values without forcing ownership from the controller.

Top-level `install` uses `desired.components` and `desired.addons` saved by `init`
in the profile's adjacent `inventory.yaml`. It installs components (and missing
component dependencies) first, then selected addons, stopping on failure while
retaining recovery inventory. An explicitly empty component selection supports
addon-only installation when its component prerequisites are already installed.
Without a saved component selection, install uses full-core defaults; addons
remain opt-in. An entirely empty selection is rejected.

Failed Helm, Kustomize and kubectl commands report their exit code and full
captured stderr/stdout, including partial output on timeout. This applies to
`kubectl apply` as well as readiness waits. Command arguments and stdin are not
added to the error, but tools can echo manifest bodies, Helm values and
credentials in their output. Diagnostics are not redacted or truncated: treat
terminal output and captured logs as sensitive, and remove secrets before
sharing them. Installation failures retain the inventory for recovery.

Top-level `uninstall` removes all inventory-recorded addons before removing
recorded components in reverse dependency order, including failed or partially
installed items, regardless of the current desired selection. Already removed
items are skipped unless `--purge` is requested to clean up their retained data.
It previews all removals before confirmation and preserves data by default.
Use `component install/uninstall` and `addon install/uninstall` for individual
operations; these commands remain available with their existing behavior.

Named installs are additive. Missing dependencies are previewed and installed in
dependency order. Already-installed dependencies are checked for ownership and
readiness, not silently upgraded; name a dependency explicitly to reconcile it.
No-target `init`, `validate` and `preflight` use the full-core defaults, with GPU
Operator only for `gpu: operator`. No-target `component install` uses the
component selection saved by `init`; without a saved selection it uses the
full-core default.
`component list` and `addon list` show all entries in the shared local inventory
with their recorded status, including failed, in-progress, and removed entries.
They do not query Kubernetes or report live health. A partially installed
component can have applied resources but still be marked `failed`; rerun its
install command to reconcile it rather than editing the inventory status.
Explicit `component install <name>` and `addon install <name>` commands also
persist their results in the profile's adjacent `inventory.yaml`, under
`components` and `addons` respectively. Component dependencies installed along
the way are recorded too, and unrelated inventory entries are preserved.
These commands do not change the `desired` selection saved by `init`; a dry run
does not record an installation.

Named uninstalls remove only those targets, in reverse dependency order, and
leave dependencies installed. Removal is refused while another active or
partially installed managed component needs a selected dependency; remove the
dependents explicitly first. There is no cascade or automatic orphan cleanup.
No-target `component uninstall` operates on all inventory-recorded components, not the
current default list. External workloads are not tracked as component dependents:
review controller users before removal.

AI Gateway without monitoring warns that traces cannot be delivered to Tempo.
It does not disable tracing or pull monitoring/auth in as dependencies.
Monitoring owns DCGM scraping; GPU Operator does not require Prometheus CRDs,
and KServe's controller does not require GPU Operator or advertised GPU capacity.

### Partial profile requirements

Only selected components being installed/reconciled require their secrets and
assets. For example, a standalone GPU installation needs no domain, TLS, Dex
connector, storage class or vendored Hub assets:

```yaml
schema_version: 1
platform: dgx-spark
context: spark
gpu: operator
```

```bash
enkictl init --component gpu-operator -p profiles/gpu/profile.yaml
enkictl component install gpu-operator -p profiles/gpu/profile.yaml
```

Use the same profile when adding other components to that installation.
`gpu-operator` rejects AKS or `gpu: existing` rather than overriding intent.
Non-GPU targets need domain/TLS. Auth loads its connector; monitoring needs
`auth.grafana_viewer_group`, and Hub needs `auth.hub_group`.
Already-installed certificate/auth dependencies do not require re-supplying
their solver/connector inputs for a consumer-only operation. Complete existing
profiles remain valid for fresh installations.

## Addons: LibreChat

LibreChat and its RAG API, PostgreSQL/pgvector database, upload storage, and
HTTPRoute are managed together as an addon in the same profile inventory as
core components. SearXNG and Crawl4AI are also managed addons:

```bash
enkictl addon install searxng -p profiles/customer/profile.yaml
enkictl addon install crawl4ai -p profiles/customer/profile.yaml
enkictl addon install librechat -p profiles/customer/profile.yaml --dry-run
enkictl addon install librechat -p profiles/customer/profile.yaml
enkictl addon uninstall librechat -p profiles/customer/profile.yaml --dry-run
enkictl addon uninstall librechat -p profiles/customer/profile.yaml
enkictl addon uninstall librechat -p profiles/customer/profile.yaml --purge
```

Install uses the profile's `chat.<domain>` hostname and requires `ai-gateway`,
`kserve` and `auth` to be installed in that profile's inventory, plus a Ready
`inference/bge-embeddings` InferenceService. It installs SearXNG, then Crawl4AI
before checking and installing LibreChat. Already installed addon dependencies
are left unchanged; name them explicitly to reconcile them. Core component
selection is not modified. Crawl4AI requires the installed `ai-gateway`
component. SearXNG has no core component dependencies.
Existing release/resource ownership is required; the CLI refuses to
adopt unmanaged resources or releases.
Dry-run renders dependencies without applying them; dependent live checks are
explicitly deferred when the dependencies still need installation.

LibreChat uses Dex-only login with PKCE and a group restriction:
`auth.chat_group`, or `auth.hub_group` when omitted. Email/password login,
local registration and public shared links are disabled. Social account
creation is available only through OpenID. The auth component owns the
`librechat` client and its callback at `https://chat.<domain>/oauth/openid/callback`.
On older CLI installations, first run `enkictl component install auth` with the
same profile to register the client, then reinstall LibreChat. The addon
checks the callback and matching credentials before upgrading. Self-signed
profiles mount the platform CA bundle without disabling TLS verification.

Addon values use profile-relative `secrets.yaml`. User-provided or generated
values are stored under `addons.librechat`; generated values are written with
owner-only permissions:

```yaml
addons:
  searxng:
    SEARXNG_SECRET: optional-existing-secret
  crawl4ai:
    CRAWL4AI_API_TOKEN: optional-existing-token
    FIRECRAWL_SHIM_API_KEY: optional-existing-shim-key
    OPENAI_API_KEY: replace-with-your-gateway-key
  librechat:
    ENKI_GATEWAY_API_KEY: replace-with-your-gateway-key
    RAG_POSTGRES_PASSWORD: optional-existing-database-password
```

The CLI reuses the existing LibreChat/RAG Secrets on rerun, otherwise creates
stable credentials and saves them locally. Crawl4AI shim and API credentials
are read from its Kubernetes Secret when not supplied locally.
`ENKI_GATEWAY_API_KEY` defaults to the current local placeholder
(`sk-local-vllm`) if omitted; set a real key if gateway authentication is
enabled. Keep `secrets.yaml` private and back it up with the cluster Secrets.

SearXNG and Crawl4AI use their fixed namespaces and profile-relative secrets.
Missing credentials are reused from the live Secret or
generated and saved privately. Configured values that differ from existing
credentials are rejected rather than silently rotated. Crawl4AI's gateway key
is named `OPENAI_API_KEY` and defaults to `sk-local-vllm`. Its gateway URL uses
the profile domain.

LibreChat's `--namespace` defaults to `librechat`; `--chart-version` defaults to
the pinned chart version, and `--rag-image` can select an alternate RAG API image.
These options and `--uploads-archive` apply only to LibreChat, not its dependencies.
Existing uploads are checked before migration. If files are still in the old
container, pass a validated backup archive with `--uploads-archive <tar>`; the
CLI refuses unsafe archive paths and never silently hides existing files with
the new PVC.

Uninstall removes tracked workload resources and the Helm release, retaining
Secrets and PVCs for recovery/reinstall. `--purge` also deletes retained
credentials and data, and requires a separate data-loss confirmation unless
`--confirm-data-loss` is supplied together with `--yes`. Local `secrets.yaml`
is not removed.
Removing LibreChat leaves its addon dependencies installed. Uninstall LibreChat
before removing SearXNG or Crawl4AI; full-profile uninstall uses dependency order.

### Legacy option retirement

| Former behavior/option | Replacement or disposition |
| --- | --- |
| Core deploy/installation scripts | Named or full-core `enkictl component install` |
| Platform/context flags | Profile `platform`/`context` |
| Skip GPU Operator | `gpu: existing`, or explicit component selection |
| Per-script dry-run | `validate --render` offline; install `--dry-run` for read-only cluster checks |
| Generated auth/env directory | Profile, connector and private local secrets |
| Chart pins and platform values | `helm/platform.yaml` and existing Helm/Kustomize assets |
| Arbitrary namespace/version flags, CRD takeover | Not exposed; fixed stack topology/pins and no adoption |
| Dashboards-only, skip-tempo, skip-crds | Not exposed; reconcile the monitoring component |
| Hub services-only/`--publish` split | Protected Hub/Zot configuration is installed together |
| Legacy Hub env-file credential/HF-token import | Not imported; manage unsupported catalog token inputs separately |
| Gateway usage-rate-limiting flag | Outside core scope; manual addon assets remain |
| SearXNG/Crawl4AI shell installers | `enkictl addon install/uninstall searxng` / `crawl4ai` |

The addon commands refuse to adopt unmanaged resources or releases. Independent
model, addon and Hub vendoring scripts remain separate.

## Configure a profile

```bash
enkictl init --profile profiles/customer/profile.yaml
enkictl validate --profile profiles/customer/profile.yaml
enkictl validate --profile profiles/customer/profile.yaml --render
```

No-target init asks for platform, context, one base domain, TLS mode, Dex connector,
credentials and groups for the active apps. It offers GitHub/Entra starters or
one arbitrary Dex connector YAML file. The review screen hides secrets and lets
you revise platform, component/addon selections, TLS or auth settings before saving.
The wizard uses interactive checkbox/select/text/password prompts when run in a
terminal. All components supported by the selected platform are checked by
default; the recommended setup is to install all components. On AKS, GPU Operator
is excluded because it is unsupported there. Addons are opt-in. Repeat
`--component NAME` to preselect a custom component set (replacing the all-selected
default). Selections are saved to the profile's `inventory.yaml`; `component
install` with no targets uses its selected components, and `addon install` with no
addon name uses its selected addons. `init` only configures local files; it does
not install cluster resources.

The only checked-in profile is [`profiles/example/`](../profiles/example/).
Customers can commit their own profile directories in forks:

| File | Treatment |
| --- | --- |
| `profile.yaml` | Non-secret desired configuration; commit in your fork |
| `connector.yaml` | Dex connector using `$NAME` references; commit without credentials |
| `secrets.yaml` | Plaintext local credentials; ignored by Git, mode 0600 |
| `inventory.yaml` | Private install selections, resource ownership and recovery state; ignored by Git, mode 0600 |
| `secrets.example.yaml` | Optional placeholder structure; safe to commit |

Back up `secrets.yaml` securely. Generated application/database credentials are
added there on first install and must not be silently rotated on subsequent runs.
The CLI records installation ownership, resource UIDs and partial progress in
`inventory.yaml`. Back up this inventory with the profile and restore it to operate
an existing installation from another checkout/machine. An absent inventory is
not permission to delete a cluster by matching resource names. Existing generated
JSON inventories remain readable and are copied to `inventory.yaml` on the next
write. Moving a profile changes its local inventory lookup path.

Fixed service origins are `https://auth.<domain>`,
`https://grafana.<domain>`, `https://hub.<domain>` and `https://zot.<domain>`.
Configure DNS/network access to the shared HTTPS Gateway separately. `ai` is
reserved for separately installed models, not published by this CLI.

DGX Spark defaults to GPU Operator with the host-provided DGX OS driver, using
`local-path` storage. Select `existing` GPU management to skip the operator.
AKS requires existing GPU management and uses `managed-csi`.
Storage/resource/runtime tuning remains in Helm and Kustomize.
The CLI pins GPU Operator v26.7.2, the version selected by the legacy floating
installer when this CLI was introduced. Chart rendering is not certification
of DGX Spark host/driver compatibility; verify that on the target machine.

## Certificate configuration

The CLI installs cert-manager and tracks solver Secrets, issuers, Certificates,
and generated TLS Secrets. There is no existing-TLS-Secret-only mode.
All modes issue the base-domain and wildcard SANs and configure the Gateway to
use the resulting Secret. Issuance must succeed before HTTPS-dependent services
are installed. cert-manager handles renewal without a CLI process running.

### Cloudflare DNS-01

```yaml
tls:
  mode: cloudflare
  acme_email: admin@your-domain.tld
  environment: production
  dns_zone: your-domain.tld
```

Put `cloudflare_api_token` in `secrets.yaml`. Scope the token to this zone with
Zone DNS Edit and Zone Read. Use `staging` explicitly to test ACME issuance;
staging certificates are not publicly trusted.

### Azure DNS-01

```yaml
tls:
  mode: azure
  acme_email: admin@your-domain.tld
  environment: production
  dns_zone: your-domain.tld
  azure:
    subscription_id: 11111111-1111-1111-1111-111111111111
    tenant_id: 22222222-2222-2222-2222-222222222222
    client_id: 33333333-3333-3333-3333-333333333333
    resource_group: dns-zone-resource-group
```

Put `azure_client_secret` in `secrets.yaml`. Grant the service principal DNS Zone
Contributor on this DNS zone only. No Azure CLI, host Azure login, or managed
identity setup is required. The solver targets Azure Public Cloud.
The authoritative DNS zone can be the base domain or a parent zone containing it.
Creating cloud credentials, cloud IAM and DNS zones is outside the CLI.

### Directly self-signed

```yaml
tls:
  mode: self-signed
```

No ACME email or DNS credentials are requested. There is no private CA hierarchy:
cert-manager directly signs the platform certificate. Self-signed mode installs
trust-manager to distribute the public certificate and a namespace-scoped
Reloader to refresh Grafana's OAuth trust after changes. Envoy OIDC/JWKS backends
use an explicit trust reference watched by Envoy Gateway. Verification is never
disabled.

The public certificate is exported beside the installation inventory as
`platform-certificate.crt`. Trust it manually on browsers and external clients.
Model-pulling node runtimes will also need trust before using self-signed Zot.
The CLI does not modify host trust stores. Renewed certificates may require
refreshing external trust; get the current public certificate from the Gateway
TLS Secret or rerun install to refresh the export. ACME staging is likewise
testing-only and does not provide public trust.

## Check and deploy

```bash
enkictl preflight -p profiles/customer/profile.yaml
enkictl install -p profiles/customer/profile.yaml --dry-run
enkictl install -p profiles/customer/profile.yaml
# Explicit confirmation for automation:
enkictl install -p profiles/customer/profile.yaml --yes
```

Install always runs validation, local/chart rendering and read-only preflight
before mutating the cluster. Preflight checks versions, API access, cluster-wide
installation permissions, Ready nodes, GPU bootstrap/existing management,
StorageClass and aggregate scheduling requests, resource conflicts and solver
inputs. It does not claim that credential presence proves DNS provider
permissions, or that StorageClass existence proves free disk space. Warnings
identify external DNS, physical disk, drivers, quotas, image compatibility and
network conditions that need operator verification.

Conflict checks list live resources once per API-group-qualified type and namespace,
and list Helm releases once per namespace. Namespaced lookups are skipped when the
namespace does not exist; cluster-scoped resources from those charts are still
checked. These read-only batches are reused only within that conflict check;
installation operations still fetch current resources before changing them.
The caller needs permission to list namespaces and the resource types being checked,
including Secrets in existing target namespaces.
Helm release lookups use explicit status filters compatible with Helm 3 and 4;
Helm 4 no longer accepts `helm list --all`.

Preflight prints its target, local manifest and Helm release counts, then
reports each remote check as it runs, including tool/Kubernetes versions,
ready/GPU node counts, configured StorageClass and aggregate CPU/memory
requests versus available capacity. A failed check still stops immediately
with its existing error; progress output does not imply checks that cannot be
verified remotely have passed.
Preflight asks kubectl to return only the node and pod fields needed for
readiness and capacity, and uses names-only output for StorageClasses and
ResourceQuotas. The pod projection excludes environment variables and Secret
references. Kubernetes still sends objects to kubectl for client-side
projection; this limits data passed to enkictl, not the API server's response
or the permissions required to list those resources.
Failed `kubectl get` requests name the resource and kubecontext, while raw
command output remains hidden to avoid exposing credentials.

Dry-run previews are read-only with respect to the cluster. Rendering may fetch
charts and update local tool caches. Secret bodies and raw failing tool output
are not printed; failures identify the phase and retain recovery inventory.
Inspect resource events and CertificateRequest/Order/Challenge status for
issuance problems, without dumping Secret contents.

On a fresh profile, `install --dry-run` renders selected addons locally but
explicitly defers their live checks until their component prerequisites are
installed. This is not a successful addon preflight. If those prerequisites are
already installed, the usual live addon preflight runs. A real install always
runs addon checks after component installation and before applying that addon.
External prerequisites (for LibreChat: SearXNG, Crawl4AI and the Ready BGE model)
remain operator-managed and can stop the addon stage; completed components stay
installed. Use individual addon commands for namespace, version, image or
uploads-migration overrides; profile-wide installation uses addon defaults.

The core install contains cert-manager, Gateway/AI Gateway, Dex,
Prometheus/Grafana/Tempo, optional Spark GPU Operator, KServe and standalone
Hub/MySQL/PostgreSQL/Zot. Top-level install also includes explicitly selected
optional addons (including LibreChat). Models remain separate. No model endpoint
or machine-authentication policy is installed.

## Uninstall and purge

```bash
enkictl uninstall -p profiles/customer/profile.yaml --dry-run
enkictl uninstall -p profiles/customer/profile.yaml
enkictl uninstall -p profiles/customer/profile.yaml --purge
# Two explicit confirmations for unattended destructive purge:
enkictl uninstall -p profiles/customer/profile.yaml --purge --yes --confirm-data-loss
```

Default uninstall retains managed PVCs, database/application credentials,
certificate private keys, ACME account Secrets and DNS solver credentials.
Certificates/issuers are removed, so certificates no longer renew until
reinstallation. Helm persistence annotations and owner-reference handling
preserve retained objects rather than relying on chart defaults.

Purge removes owned retained data/Secrets and removes CLI-created CRDs only when
cluster-wide checks prove they have no instances. Unverifiable or still-used
CRDs are preserved with a warning. A deleted PVC can destroy its backing disk
or leave a retained PV according to the storage reclaim policy. No namespaces
are blanket-deleted, no external resources are adopted, and no finalizers are
force-removed. Local `secrets.yaml` and recovery inventory remain available.

## Development and future distribution

```bash
python -m pip install -e '.[dev]'
python -m pytest cli/tests
python -m ruff check cli
python -m ruff format --check cli
python -m build
```

The chart rendering test fetches pinned upstream charts but never contacts a
Kubernetes cluster. Unit tests use stubbed cluster operations. Real browser
login, GPU readiness, certificate issuance/renewal and live install/uninstall
still need acceptance testing on a disposable cluster.

Source lives under `cli/enkictl/`, with root packaging metadata. Asset lookup
is separate from Python imports in preparation for later PyInstaller packaging.
Standalone binary builds and bundling Helm/kubectl/manifests are not part of this
release. Helm 4 uses a bundled, process-local post-renderer plugin descriptor;
the CLI does not install plugins into your global Helm configuration.
