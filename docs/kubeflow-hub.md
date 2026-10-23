# Kubeflow Hub

Hub is a standalone component, not the full Kubeflow distribution. Resources
come from the vendored Hub v0.3.17 manifests under `k8s/kubeflow-hub/upstream`.
Keep that directory unchanged; stack-specific changes belong in the local base,
catalog and platform overlays. If upstream is absent, populate it with
`bash scripts/vendor-kubeflow-hub.sh` before rendering.

## Architecture

- Namespace: `kubeflow` for Hub, databases, Zot and authentication, with scoped
  job bindings/credentials in `default` and pull credentials in `inference`.
- Registry: `model-registry-service.kubeflow.svc.cluster.local:8080`.
- Registry database: upstream MySQL 8.3, with the `metadata-mysql` 10 GiB RWO PVC.
- Catalog: upstream `model-catalog-server` and `model-catalog` ClusterIP service.
- Catalog database: separate PostgreSQL 17.6 Alpine instance with a
  `model-catalog-postgres` 10 GiB RWO PVC. The Alpine image matches upstream's
  UID/GID 70. This is not the registry's database.
- UI: upstream UI base, with the `ui-standalone:v0.3.17` image and standalone
  backend mode enabled locally. The generic `ui` image is compiled for the full
  Kubeflow dashboard; setting the backend flag alone does not change frontend
  mode and causes `window.centraldashboard is undefined`.
- Public origin: `https://hub.zer0.garden`, protected by Dex on the existing gateway.
- Identity: Dex client `hub`, required group `enki-farm:core`.
- Serving: existing KServe installation and inference workloads in `inference`.
- OCI storage: Zot v2.1.22, `zot-data` 100 GiB RWO PVC, authenticated
  `https://zot.zer0.garden/v2/` on the existing gateway.

The upstream standalone overlay is not used: it injects a demo identity through
nginx and grants that identity cluster-admin. Instead, the UI service sends
traffic directly to port 8080 on the UI container. Envoy verifies identity and
retains a `/model-registry` prefix rewrite for compatibility with old frontend
asset URLs. The standalone frontend requests `/app.bundle.js` directly; the
generic Kubeflow frontend requested `/model-registry/app.bundle.js`, which
returned HTML without that rewrite. Use the standalone image rather than
relying on a rewrite to turn the generic image into a standalone frontend.
Both route rules set `Cache-Control: private, no-store` because Hub is
authenticated. If Cloudflare cached an earlier HTML response under a JavaScript
URL, purge that URL from Cloudflare as well: new origin headers cannot evict an
existing edge-cache entry. Configure a Cloudflare cache bypass rule for
`hub.zer0.garden` if an edge rule overrides origin cache headers.
It forwards the verified email as `kubeflow-userid`. A namespace-scoped reader Role
grants the authorized Dex group namespace and service discovery, without
cluster-admin. The UI ServiceAccount retains upstream service discovery and
SubjectAccessReview permissions, plus namespace discovery.

Register-and-store namespace checks impersonate the selected namespace's
`default` ServiceAccount and require `get` on the registry Service. A local
`hub-job-registry-reader` RoleBinding grants this to `kubeflow/default`,
limited to `model-registry-service` in `kubeflow`. It does
not grant access to Secrets, job creation, other Services or cluster-admin.
The installer applies transfer-job RBAC only in `kubeflow` and removes the
previous Hub-owned discovery/job Roles, RoleBindings and `zot-push` Secret
from `default`.
The UI ServiceAccount can manage Jobs and their supporting ConfigMaps/Secrets
and read pod/event status in that namespace. This is intentionally more than
the discovery Role: core members can launch arbitrary transfer jobs through Hub,
so this is a trusted job namespace. Do not store unrelated sensitive Secrets
there if Hub users must not access them. No cluster-wide job or Secret access
is granted.

On **Model transfer jobs**, enter `kubeflow` in **Transfer job namespace** and
press Enter. Upstream v0.3.17 initially requests all namespaces and displays a
permission notice before showing this field. Namespaced job listing is allowed;
cluster-wide listing is deliberately not. The standalone UI has no supported
default for this field. Other namespace access granted outside this installer
can still make additional namespaces visible.

The frontend derives its registry request's `namespace` from its namespace
selector, not from the browser URL. The group Role includes `get namespaces` in
`kubeflow` so the BFF's per-namespace SubjectAccessReview succeeds. Adding a URL
query parameter does not repair an empty selector.

This installation uses Zot OCI images rather than SeaweedFS and does not manage
`hub-artifact-credentials`. Use direct `oci://` storage URIs for KServe modelcars;
do not send OCI images through the Hub `model-registry://` Go initializer.
Direct KServe PVC serving is a separate path. Existing Hugging Face cache
volumes are unchanged.

## Zot and Register-and-Store

Unmodified upstream Helm defaults are stored in
[values-zot.yaml](../helm/zot/values-zot.yaml), from chart `zot` version `0.1.128`
(app version `v2.1.22`), repository `https://zotregistry.dev/helm-charts`.
Refresh that snapshot with:

```bash
helm show values zot --repo https://zotregistry.dev/helm-charts --version 0.1.128 > helm/zot/values-zot.yaml
```

These are reference defaults, not this stack's deployment overrides. The Hub
installer still deploys Zot through the local Kustomize manifests.

Zot has no Dex/browser authentication. It is a machine registry with HTTPS and
Basic authentication; no anonymous repository access is allowed. Only `models/**`
is writable by the `hub` account. The `kserve` account can only read that prefix.
The ClusterIP backend accepts ingress only from the Envoy gateway namespace.
Zot data remains in `kubeflow`; the installer copies Docker-format credentials
to job/serving namespaces without changing their default ServiceAccounts.

KServe modelcars are pulled by the node container runtime, not a Pod HTTP client.
The registry therefore uses the existing publicly trusted wildcard certificate
and resolvable hostname, avoiding insecure-registry or private-CA changes on
every node. The route is authenticated but publicly reachable. On other clusters,
configure DNS/TLS and the gateway parent for `zot.zer0.garden` before use.

In Hub, select registry namespace **kubeflow**, registry **model-registry-service**,
then **Register and store**. For the destination enter:

| Field | Value |
|---|---|
| Job namespace | `kubeflow` |
| Registry | `zot.zer0.garden` |
| URI | `zot.zer0.garden/models/<model-name>:<immutable-version>` |
| Username | `hub` |
| Password | `hub-password` from Secret `kubeflow/zot-auth` |

Do not include `https://` or `oci://` in the destination form fields. Retrieve
the password privately as an administrator; never paste it into Git, logs or
chat. For example, on macOS copy it directly to the clipboard:

```bash
kubectl --context spark -n kubeflow get secret zot-auth \
  -o 'jsonpath={.data.hub-password}' | base64 -d | pbcopy
```

Paste the decoded password, not the base64 value from the Secret YAML, and use
the `hub` account rather than Dex credentials or the read-only `kserve` password.
Zot's `htpasswd bcrypt compare failed` log indicates a password mismatch;
recreate the transfer job with the current password rather than rotating it.
Hub snapshots form credentials into a job-owned Secret, so editing the form or
the shared `zot-push` Secret does not repair an already submitted job.

Hub v0.3.17's standalone UI requires destination fields and credentials per job;
it has no supported setting to prepopulate a registry or select an existing
credential Secret. The installer provisions `zot-push` in
`kubeflow` for other OCI clients; Hub itself creates a job-owned destination
Secret from the form values. No custom frontend, demo login, secret injection
into HTML, or upstream modification is used.

The upstream standalone BFF also hardcodes `async-upload:latest` for transfer
Jobs; its image ConfigMap override is only read in federated mode. Static stack
images remain versioned. Pinning dynamic transfer-job images would require an
upstream fix or a separately reviewed admission policy.

### Serving a stored OCI model

The installer provides `zot-model-puller` ServiceAccounts and read-only
`zot-pull` image-pull Secrets in `kubeflow` and `inference`. Choose a runtime
appropriate to the model. An sklearn example is:

```yaml
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: example-model
  namespace: inference
  annotations:
    serving.kserve.io/deploymentMode: Standard
spec:
  predictor:
    serviceAccountName: zot-model-puller
    model:
      runtime: kserve-sklearnserver
      modelFormat:
        name: sklearn
      storageUri: oci://zot.zer0.garden/models/example-model@sha256:REPLACE_WITH_MANIFEST_DIGEST
```

Prefer manifest digests for serving, so reusing a tag cannot silently change a
model. The installed KServe configuration must have `enableModelcar: true`.
The end-to-end test checks a real node pull and inference, not only registry HTTP.

### Registry ingress and operations

Zot publishes only `/v2`, with 30-minute gateway upload timeouts and
`Cache-Control: private, no-store`. Add a Cloudflare cache bypass rule for
`zot.zer0.garden`. **Cloudflare's request-body size and timeout limits still
apply:** for large model layers use direct/split-horizon DNS to the TLS gateway
(or a DNS-only private ingress), preserving this hostname and trusted TLS.
Changing Envoy timeouts does not remove a CDN upload-size limit. Small-model
tests through the current Cloudflare path do not certify multi-GB uploads.

Credentials are generated once and reused from `zot-auth`; an existing Zot PVC
without that Secret causes installation to fail rather than regenerate them.
Back up both. Rotating accounts requires coordinated updates to Zot, Hub job
inputs and copied image-pull Secrets. Zot is single-replica with Recreate
updates, not HA. Deletion and garbage collection are deliberately disabled for
these accounts; old tags/layers consume storage until an administrator performs
an explicit retention/cleanup procedure.

## Model Catalog

Sources are configured in
[catalog/sources.yaml](../k8s/kubeflow-hub/catalog/sources.yaml). The configured
model, MCP and agent catalog files are mounted together; upstream also supplies
its separate MCP and skill source ConfigMaps. Catalog discovery does not download
model weights, copy artifacts or deploy models.

For authenticated Hugging Face access, set `HF_API_KEY` in the ignored
`k8s/kubeflow-hub/.env`, or pass `--env-file <path>`. The installer creates or
updates `model-catalog-hf-api-key`. An absent or empty token preserves any
existing Secret. The installer restarts the catalog after its database is ready
so it reloads database credentials and the HF token from Secrets even when the
Deployment spec has not changed. When updating a Secret outside the installer,
reload it with:

```bash
kubectl --context spark -n kubeflow rollout restart deployment/model-catalog-server
```

Do not put tokens in Git. Supplying a token does not grant gated-model access.
All users with catalog access can see metadata ingested with that token.

## Installation

The installer requires `kubectl`, `openssl`, `jq` and bcrypt-capable `htpasswd`
(typically supplied by `apache2-utils` or `httpd-tools` on Linux). It uses
standalone `kustomize` when available, otherwise `kubectl kustomize`.

Configure these values in the ignored Dex auth configuration:

```dotenv
HUB_URL=https://hub.zer0.garden
HUB_GROUP=enki-farm:core
```

Render auth and preview installation without accessing the cluster:

```bash
bash scripts/install-auth.sh --output-dir .generated/auth --dry-run
bash scripts/install-kubeflow-hub.sh --platform dgx-spark --auth-dir .generated/auth --dry-run
```

Install Hub services and the authenticated Zot endpoint, without restarting Dex
or publishing the Hub browser route:

```bash
bash scripts/install-kubeflow-hub.sh --platform dgx-spark --context spark --auth-dir .generated/auth
```

Install the Dex client if it is not already installed, then publish:

```bash
bash scripts/install-auth.sh --output-dir .generated/auth
bash scripts/install-kubeflow-hub.sh --platform dgx-spark --context spark --auth-dir .generated/auth --publish
```

Auth installation uses the current context and restarts Dex. If the Hub Dex
client is already installed, only rerender auth with `--dry-run` before publishing;
this updates the generated route namespace without restarting Dex.

Both the HTTPRoute and SecurityPolicy now live in `kubeflow`, as does `hub-oidc`.
An old generated route targeting `kubeflow-hub` is rejected with a rerender
instruction. The installer also rejects stale generated routes without the
frontend asset rewrite. Rerender
into the same `--output-dir` used as the Hub installer's `--auth-dir` so a later
`--publish` does not restore an obsolete route.
It waits for HTTPRoute `Accepted` and `ResolvedRefs`, then SecurityPolicy
acceptance. These conditions do not replace real browser
login, callback, refresh, logout and nonmember checks.

Dex authorization and token endpoints are explicitly configured in the Hub
policy. This avoids gateway configuration depending on issuer discovery while
Dex is restarting. A rejected OIDC policy can leave JWT checks active without
the browser login flow, producing `Jwt is missing`; inspect SecurityPolicy
acceptance rather than disabling JWT validation.

Spark uses `local-path`; `--platform aks` uses `managed-csi`. Both databases are
single-instance, not HA. Passwords are generated once, stored in Kubernetes
Secrets and reused; read errors and missing credentials for existing PVCs abort
installation. Changing a Secret alone does not rotate an initialized database
password. Upstream-generated default Secrets and empty-password settings are
removed by local patches.

This is a fresh MySQL installation, **not a PostgreSQL migration**. The installer
does not delete old workloads, data, routes or cluster-admin bindings. Before
publishing, remove any obsolete same-host route in `kubeflow-hub` and inspect the
old `service-access-cluster-binding` demo/admin binding. Existing default or
incompatible database credentials require an explicit reset or credential
restoration; the installer will not silently overwrite them.

The initializer may be repointed from this stack's old `kubeflow-hub` registry
address to `kubeflow`. An initializer owned by another registry is rejected.

## Backup and Rollback

Back up MySQL and catalog PostgreSQL independently, including credentials.
Back up Zot's persistent OCI storage and `zot-auth` as well; automated backups
are not installed. On rollback,
remove only the Hub route and policy first; retain database PVCs and Secrets.
Do not use `kubectl delete -k` on the whole component, as that deletes PVCs.
Do not remove the storage initializer while inference workloads depend on it.

## Later Improvements

- Encrypted off-node database backups and tested restoration.
- Zot retention, capacity monitoring, off-node backups and HA where needed.
- Direct registry DNS/ingress suitable for multi-GB OCI layers.
- Network isolation and private TLS appropriate to the cluster threat model.
- Real browser OIDC callback, refresh and logout verification before production.
