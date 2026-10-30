# LibreChat With Local RAG

The `enkictl addon` commands manage the LibreChat Helm release plus a standalone
RAG API Deployment, PostgreSQL/pgvector StatefulSet and internal Services in
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