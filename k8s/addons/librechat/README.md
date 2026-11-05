# LibreChat With Local RAG

The `enkictl addon` commands manage the LibreChat Helm release plus a standalone
RAG API Deployment, PostgreSQL/pgvector StatefulSet, artifact bundler Deployment
and internal Services in
the LibreChat namespace. The upstream RAG subchart stays disabled. Chat
generation still uses Enki Gateway; embeddings run locally on CPU.

## Prerequisites

- `ai-gateway`, `kserve` and `auth` installed by `enkictl` using the same profile.
- `auth.chat_group` or `auth.hub_group` for the required Dex group.
- SearXNG and Crawl4AI are installed automatically as CLI addon dependencies.
- Independently installed [BGE model](../../../models/bge-embeddings/README.md).
- Default storage class; this first version targets ARM64 DGX Spark.
- Dex-only login is managed by the CLI.

The API uses the unmodified upstream
`registry.librechat.ai/librechat-ai/librechat-rag-api-dev-lite` image, pinned by
digest for reproducibility. No custom build or image publication is required.
Use `--rag-image` to select another upstream version when upgrading.
`EMBEDDINGS_PROVIDER=openai`, `EMBEDDINGS_MODEL=embedding`, and
`RAG_OPENAI_BASEURL=http://bge-embeddings-predictor.inference.svc.cluster.local/v1`
connect its OpenAI embeddings client directly to the BGE InferenceService.
The nonsecret API-key placeholder satisfies the client SDK; it is not a
credential for an external provider. `RAG_CHECK_EMBEDDING_CTX_LENGTH=false`
prevents OpenAI-specific tokenization of inputs for this local model.

```bash
kubectl apply -k models/bge-embeddings
kubectl wait -n inference --for=condition=Ready inferenceservice/bge-embeddings --timeout=15m
enkictl addon install librechat -p profiles/customer/profile.yaml --dry-run
enkictl addon install librechat -p profiles/customer/profile.yaml
```

The addon uses `chat.<domain>` and the AI Gateway URL derived from the selected
profile. Existing resources or Helm releases not owned by that profile's CLI
inventory are not adopted. The CLI configures Dex-only login, disabling local
login/registration and public shared links. Access requires `auth.chat_group`,
falling back to `auth.hub_group`. For older CLI deployments, reconcile
`enkictl component install auth` with the same profile before reinstalling
LibreChat to register its Dex client and callback.
`--namespace` applies to LibreChat resources; SearXNG and Crawl4AI retain their
fixed namespaces and the embedding model always remains in `inference`.
`--chart-version` selects another LibreChat chart version and
`--rag-image` overrides the pinned RAG API image. Dry-run performs read-only
preflight and rendering without applying cluster resources or saving generated
secrets. If addon dependencies are not installed yet, dry-run renders them and
defers LibreChat's live preflight, explicitly reporting that readiness is unverified.

The addon command checks model readiness, preserves/generates credentials, applies
RAG resources, waits for PostgreSQL, verifies database authentication, waits
for the API, and tests a real embedding from the API pod before upgrading
LibreChat. A failed check prevents the Helm upgrade, but already applied RAG
resources are retained for diagnosis. Reinstalls restart the API to pick up
ConfigMap or secret changes.

Credential inputs live under `addons.librechat` in the profile-relative
`secrets.yaml`. The CLI preserves existing Kubernetes Secrets on rerun and
writes generated values locally with restricted permissions. Crawl4AI's API
token and Firecrawl shim key are read from its `crawl4ai-credentials` Secret if
not supplied locally.

## Self-hosted Artifacts

Installation also provisions the telemetry-removed
[LibreChat CodeSandbox fork](https://github.com/LibreChat-AI/codesandbox-client),
pinned to a verified multiarchitecture image digest (AMD64 and ARM64). Its
Service is ClusterIP only, with no NodePort or LoadBalancer. On CNIs that enforce
NetworkPolicy, only the Envoy data-plane namespace can reach the bundler.
The container runs nonroot with a read-only root filesystem.

Previews are loaded by the user's browser, not the LibreChat backend. Two
HTTPS routes are therefore required:

| Origin | Runtime |
| --- | --- |
| `bundler.<domain>` | React bundler; also the top-level login entry point |
| `*.bundler.<domain>` | Per-preview static HTML, SVG, Markdown and file previews |

Static previews use Sandpack's separate `static-browser-server` service-worker
protocol, not the React bundler. An unprivileged init container downloads
`static-browser-server@1.0.3` from the npm registry, verifies its pinned SHA-512,
and extracts only its preview assets into an ephemeral volume. Registry access
is needed on each new pod; a failed download or checksum blocks startup.
Both runtimes are served by the same Nginx Deployment on separate internal ports.
`SANDPACK_BUNDLER_URL` and `SANDPACK_STATIC_BUNDLER_URL` are set independently;
neither falls back to CodeSandbox's public preview service. Mermaid renders
directly in LibreChat. This is not a fully offline setup: React dependencies,
fonts and Tailwind may still need public CDNs.

Both routes share an Envoy OIDC/JWT policy using a separate Dex client `bundler`,
callback `https://bundler.<domain>/oauth2/callback`, and the same required group as
LibreChat (`auth.chat_group`, falling back to `auth.hub_group`). Access defaults
to deny; the JWT audience and group must match. Tokens are stripped before
forwarding to Nginx. Login cookies are scoped to `bundler.<domain>` and its
preview subdomains, **not** to the base domain or LibreChat. Responses disable
shared caching, and only `https://chat.<domain>` may frame the runtime.

Before upgrading an older installation:

1. Configure DNS for `bundler.<domain>` and `*.bundler.<domain>` to reach the
   shared HTTPS Gateway. A tunnel must forward these hostnames unchanged and
   must not cache authenticated bundler responses.
2. Reconcile certificates and Dex with the same profile:

   ```bash
   enkictl component install certificates auth -p profiles/customer/profile.yaml
   enkictl addon install librechat -p profiles/customer/profile.yaml --dry-run
   enkictl addon install librechat -p profiles/customer/profile.yaml
   ```

   The platform Certificate now also covers `*.bundler.<domain>`; the ordinary
   `*.<domain>` wildcard does not cover nested preview hosts. Preflight rejects
   the old certificate or a missing bundler client with upgrade guidance.
   For self-signed TLS, trust the reissued certificate on client machines.
3. Open `https://bundler.<domain>` in a **top-level tab** and complete Dex login.
   The blank sandbox or sandbox-ID error after login is normal without an
   embedding editor. Return to LibreChat and reload previews. Repeat this step
   if the bundler session expires; identity-provider login inside an iframe
   may be blocked by browser framing policies.
4. In the Agent Builder, add **Artifacts** to the agent's tools and save it.
   LibreChat's default artifact capability remains enabled; agents still need
   the tool selected. See the upstream [feature guide](https://www.librechat.ai/docs/features/artifacts)
   and [configuration guide](https://www.librechat.ai/docs/configuration/artifacts).

The installer tracks the bundler resources and credentials in the addon
inventory, waits for the Deployment and both protected routes, and requires
policy acceptance before upgrading LibreChat. Uninstall removes the workload,
routes and policy through that inventory; normal secret-retention rules apply.
The stable Dex identity remains owned by `auth`.

Before publishing, verify a member can log in and preview both a React artifact
and an HTML/SVG/Markdown artifact. In browser network tools, ensure previews use
the configured bundler hosts rather than CodeSandbox preview hosts. Verify an
anonymous request is redirected to Dex (or denied), a nonmember is denied, and
forged token headers do not bypass authentication on either route. These
interactive checks require a deployed Gateway, DNS and an identity-provider
session; manifest/unit tests cannot establish them.

## Existing Upload Migration
## Code Interpreter

Code execution is a separate, optional CodeAPI service; it is not the browser
Artifacts bundler. To use it on DGX Spark, first authenticate Docker to GHCR
with permission to publish packages, then build the pinned upstream ARM64
images:

```bash
docker login ghcr.io
bash scripts/build-code-interpreter-images.sh
enkictl addon install librechat -p profiles/customer/profile.yaml \
  --with-code-interpreter --dry-run
enkictl addon install librechat -p profiles/customer/profile.yaml \
  --with-code-interpreter
```

The builder pins Code Interpreter `v1.10.13` at commit
`2fa9f0d43e8e38b7d6240bc76e2483c6a47c64c7` and publishes the API, worker,
direct sandbox runner, file server, egress gateway and package-init images under
`ghcr.io/enki-farm` with tag `v1.10.13-arm64`. It leaves an already-published
tag unchanged. The chart asset is pinned to CodeAPI chart `0.3.2` from the same
release. Redis/MinIO chart dependencies also need ARM64-compatible images on
the target node; validate their actual pulls and readiness before relying on
the service.

Install the upstream `seccomp/nsjail.json` from that release on every node that
may schedule CodeAPI, at
`/var/lib/kubelet/seccomp/profiles/nsjail.json`, using node configuration
management. The addon enables the chart's Localhost seccomp profile and runs a
read-only, unprivileged DaemonSet check before installing CodeAPI; it does not
write files onto hosts. If the profile is missing, CodeAPI installation must
not proceed.

The sandbox uses direct NsJail (`kvmEnabled=false`), has networking disabled,
and shares the host kernel. Access remains limited to the configured Dex chat
group; this is for trusted users only, not public or untrusted execution. JWTs
are minted per LibreChat user. Programmatic tool calling, background execution
and stateful sessions remain disabled. CodeAPI has no public route, and its API
NetworkPolicy admits only the LibreChat pod.

Redis, MinIO and runtime packages use persistent `local-path` claims (1Gi,
5Gi and 10Gi respectively); worker scratch remains ephemeral. LibreChat's
explicit file deletion and expired-file sweep delete corresponding CodeAPI
objects. The addon does not set a retention policy, and conversation deletion
alone has not been verified to remove those file records. Configure and verify
LibreChat file expiry before production use; do not assume artifacts are
temporary per conversation under the default retention settings. Normal addon
uninstall retains CodeAPI credentials and claims; `--purge` removes managed
data after confirmation.

## Existing Upload Migration

The new 10Gi PVC mounts `/app/uploads`. Existing files in the old container
must not be hidden by that mount. The addon refuses an upgrade while such
files exist unless a backup archive is supplied.

During a maintenance window, stop user uploads and archive the running pod's
files before any rollout. Keep this archive securely outside the repository:

```bash
umask 077
kubectl exec -n librechat deployment/librechat-librechat -- \
  tar -C /app/uploads -cf - . > /secure/backup/librechat-uploads.tar
tar -tf /secure/backup/librechat-uploads.tar
enkictl addon install librechat -p profiles/customer/profile.yaml \
  --uploads-archive /secure/backup/librechat-uploads.tar
```

Supply only an archive you created and trust. The addon restores it into the new
PVC using a temporary nonroot pod before the Helm upgrade, then deletes that
pod. Do not resume uploads until installation completes. Preserve the archive
until file access is verified. Re-running archive restoration after the upload
mount is active is rejected. LibreChat uses Recreate updates for its
single-replica RWO storage, so upgrades include a short interruption.
When upgrading an existing RollingUpdate Deployment, the addon first
atomically sets Recreate and removes its old rollingUpdate fields. This avoids
Helm server-side apply retaining an invalid combination of both strategies.
The strategy-only migration does not restart pods; the subsequent Helm upgrade
performs the rollout.

## Storage And Credentials

| Resource | Size | Purpose |
| --- | --- | --- |
| `data-librechat-rag-db-0` | 20Gi | PostgreSQL vectors and chunk content |
| `librechat-uploads` | 10Gi | Original host-storage uploads |
| Existing `huggingface-cache` in `inference` | 100Gi shared | Model cache; not created by this addon |
| API staging `emptyDir` | 1Gi limit | Temporary ingestion files |

Existing MongoDB, image and Meilisearch volumes are unchanged. RAG staging
does not share LibreChat's upload PVC; the client sends files over HTTP and
upstream removes staging files after ingestion. Local-path volumes survive
pod restarts but are not protection against node/disk loss. Deleting PVCs
can destroy data according to the storage class reclaim policy; do not remove
them during routine upgrades. StatefulSet claim retention defaults to Retain.

The database password is read from `addons.librechat.RAG_POSTGRES_PASSWORD` in
the profile-relative `secrets.yaml`, otherwise reused from
`librechat-rag-db-credentials`, otherwise generated. The CLI refuses mismatched passwords and an existing database
PVC without its Secret. PostgreSQL only applies `POSTGRES_PASSWORD` on first
initialization: perform role rotation explicitly, update the Secret and
restart consumers. Keep the original Secret for disaster recovery.

Uninstall retains credentials and PVCs. To intentionally delete that data, use
`enkictl addon uninstall librechat -p profiles/customer/profile.yaml --purge`;
interactive confirmation is required, or pass `--yes --confirm-data-loss`.
Local `secrets.yaml` is never deleted by purge.

The API reads only `JWT_SECRET` from `librechat-credentials-env`; it does not
receive unrelated chat or crawler credentials. Services have no public routes.
ClusterIP alone is not a network authorization policy. Deploy NetworkPolicies
only with a CNI that enforces them if stronger pod-level isolation is needed.

## Backup And Restore

Pause ingestion and deletion during coordinated backups. Back up PostgreSQL,
LibreChat MongoDB, original upload files, credential Secrets and the pinned
model/image configuration. Store encrypted backups outside the Spark node;
the model cache is reproducible and does not need backup.

```bash
umask 077
kubectl exec -n librechat statefulset/librechat-rag-db -- \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > /secure/backup/librechat-rag.dump
```

Exercise restore into a disposable database before replacing production data:

```bash
kubectl exec -n librechat statefulset/librechat-rag-db -- \
  sh -c 'createdb -U "$POSTGRES_USER" rag_restore_check'
kubectl exec -i -n librechat statefulset/librechat-rag-db -- \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d rag_restore_check --no-owner --exit-on-error' \
  < /secure/backup/librechat-rag.dump
kubectl exec -n librechat statefulset/librechat-rag-db -- \
  sh -c 'psql -U "$POSTGRES_USER" -d rag_restore_check -c "SELECT count(*) FROM langchain_pg_embedding"'
```

Production restoration requires maintenance mode, restored matching Secrets,
MongoDB and uploads, a reviewed database restore, then API/LibreChat restart
and upload/retrieval/deletion checks. Reusing existing vectors after changing
the embedding model is unsupported; select a new collection and reindex.

## Tests And Troubleshooting

```bash
python -m pytest cli/tests/test_addons.py cli/tests/test_lifecycle.py
python -m pytest cli/tests/test_components.py
kubectl logs -n librechat deployment/librechat-rag-api
kubectl logs -n librechat statefulset/librechat-rag-db
kubectl logs -n inference deployment/bge-embeddings-predictor
```

The addon tests cover composition, credential handling, archive validation and
ownership retention; the separate model smoke test exercises actual CPU vLLM.
After deployment, check PDF/Markdown/TXT uploads, retrieval of a unique fact,
file reuse/deletion, cross-user isolation, and persistence across pod restarts.
Verify SSO and web search still work. Those UI/cluster checks are not replaced
by configuration tests.

First scope is personal chat attachments, English text PDFs, Markdown and
plain text. No OCR, reranker, automated backup schedule or HA is included.
API behavior, including ownership checks and agent support, is provided by
upstream without local overrides. Agent knowledge-base deletion compatibility
must be checked before using it with the pinned LibreChat client.

Upstream `CHUNK_SIZE=400` and `CHUNK_OVERLAP=40` count characters, not BGE
tokens. They are initial configuration values, not a token-budget guarantee.
BGE accepts at most 512 tokens, including special tokens. Test representative
documents and queries; dense text or long queries can exceed that limit.
The API is not modified to add BGE query instructions or tokenizer-aware
splitting, and disabling OpenAI context-length checking does not remove the
model's input limit.