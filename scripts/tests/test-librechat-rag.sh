#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/bin" "$TEST_DIR/addon"
cp -R "$ROOT_DIR/k8s/addons/librechat/" "$TEST_DIR/addon/"
export REAL_KUBECTL="$(command -v kubectl)"
export TEST_LOG="$TEST_DIR/commands.log"
export TEST_MODEL_READY=True TEST_UPLOADS=0 TEST_MOUNTED="" TEST_DB_EXISTS="" TEST_DB_SECRET=""
export TEST_STRATEGY=RollingUpdate

cat > "$TEST_DIR/bin/kubectl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
for argument in "$@"; do
  case "$argument" in --from-literal=*) printf '%s ' '--from-literal=REDACTED' >> "$TEST_LOG" ;; *) printf '%s ' "$argument" >> "$TEST_LOG" ;; esac
done
printf '\n' >> "$TEST_LOG"
case "$*" in
  kustomize*) exec "$REAL_KUBECTL" "$@" ;;
  *'apply -f -'*) cat >/dev/null ;;
  *'get inferenceservice bge-embeddings'*) printf '%s' "$TEST_MODEL_READY" ;;
  *'get deployment librechat-librechat'*volumeMounts*) printf '%s' "$TEST_MOUNTED" ;;
  *'get deployment librechat-librechat'*persistentVolumeClaim*) [[ -z "$TEST_MOUNTED" ]] || printf librechat-uploads ;;
  *'get deployment librechat-librechat'*spec.strategy.type*) printf '%s' "$TEST_STRATEGY" ;;
  *'get deployment librechat-librechat'*) printf librechat-librechat ;;
  *'exec deployment/librechat-librechat'*) printf '%s' "$TEST_UPLOADS" ;;
  *'get pvc data-librechat-rag-db-0'*) printf '%s' "$TEST_DB_EXISTS" ;;
  *'get secret librechat-rag-db-credentials'*) printf '%s' "$TEST_DB_SECRET" | base64 ;;
  *'get secret oidc-credentials'*) ;;
  *'get secret'*CRAWL4AI_API_TOKEN*) printf crawl-token | base64 ;;
  *'get secret'*) printf stable-credential | base64 ;;
  *'create namespace'*|*'create secret'*) printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: test\n' ;;
  *'rollout status deployment/librechat-rag-api'*) [[ "${TEST_API_FAIL:-}" != true ]] ;;
  *'exec -i librechat-upload-restore'*) cat >/dev/null ;;
esac
EOF
cat > "$TEST_DIR/bin/helm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'helm %s\n' "$*" >> "$TEST_LOG"
EOF
chmod +x "$TEST_DIR/bin/kubectl" "$TEST_DIR/bin/helm"
export PATH="$TEST_DIR/bin:$PATH"
INSTALLER="$TEST_DIR/addon/install-librechat.sh"

expect_failure() {
  if bash "$INSTALLER" "$@" > "$TEST_DIR/failure.log" 2>&1; then
    printf 'Expected installer failure\n' >&2
    exit 1
  fi
}

bash "$INSTALLER" --dry-run --namespace rag-test > "$TEST_DIR/dry-run.log"
command grep -q 'namespace: rag-test' "$TEST_DIR/dry-run.log"
command grep -q 'image: registry.librechat.ai/librechat-ai/librechat-rag-api-dev-lite@sha256:' "$TEST_DIR/dry-run.log"
command grep -q 'RAG_OPENAI_BASEURL: http://bge-embeddings-predictor.inference.svc.cluster.local/v1' "$TEST_DIR/dry-run.log"
if command grep -Eq 'get |exec |rollout |apply |create ' "$TEST_LOG"; then
  printf 'Dry-run accessed or changed the cluster\n' >&2
  exit 1
fi
expect_failure --namespace
expect_failure --rag-image
expect_failure --uploads-archive
expect_failure --namespace 'bad namespace'
expect_failure --rag-image 'bad image'

export TEST_MODEL_READY=False
expect_failure
command grep -q 'not Ready' "$TEST_DIR/failure.log"
export TEST_MODEL_READY=True TEST_UPLOADS=1
expect_failure
command grep -q 'Existing uploads need migration' "$TEST_DIR/failure.log"
export TEST_UPLOADS=0 TEST_DB_EXISTS=data-librechat-rag-db-0
expect_failure
command grep -q 'restore the original Secret' "$TEST_DIR/failure.log"
export TEST_DB_EXISTS="" TEST_DB_SECRET=stable-db-password
printf 'RAG_POSTGRES_PASSWORD=different\n' > "$TEST_DIR/addon/.env"
expect_failure
command grep -q 'password differs' "$TEST_DIR/failure.log"
rm "$TEST_DIR/addon/.env"

: > "$TEST_LOG"
bash "$INSTALLER" --namespace rag-test --rag-image example.test/rag:bge > "$TEST_DIR/install.log"
command grep -q -- '--namespace rag-test' "$TEST_LOG"
command grep -q 'rollout status statefulset/librechat-rag-db' "$TEST_LOG"
command grep -q -- '--set-string librechat.configEnv.RAG_API_URL=http://librechat-rag-api:8000' "$TEST_LOG"
database_line="$(command grep -n 'rollout status statefulset' "$TEST_LOG" | head -n 1 | cut -d: -f1)"
api_line="$(command grep -n 'rollout status deployment/librechat-rag-api' "$TEST_LOG" | head -n 1 | cut -d: -f1)"
helm_line="$(command grep -n 'helm upgrade' "$TEST_LOG" | cut -d: -f1)"
[[ "$database_line" -lt "$api_line" && "$api_line" -lt "$helm_line" ]]
patch_line="$(command grep -n 'patch deployment librechat-librechat --type=merge' "$TEST_LOG" | cut -d: -f1)"
[[ "$api_line" -lt "$patch_line" && "$patch_line" -lt "$helm_line" ]]
command grep -Fq '"strategy":{"type":"Recreate","rollingUpdate":null}' "$TEST_LOG"
if command grep -q stable-db-password "$TEST_DIR/install.log" "$TEST_LOG"; then
  printf 'Database password leaked\n' >&2
  exit 1
fi
export TEST_DB_EXISTS=data-librechat-rag-db-0 TEST_MOUNTED=uploads
export TEST_STRATEGY=Recreate
: > "$TEST_LOG"
bash "$INSTALLER" > "$TEST_DIR/reinstall.log"
if command grep -q 'patch deployment' "$TEST_LOG"; then
  printf 'Recreate strategy was unnecessarily patched\n' >&2
  exit 1
fi
export TEST_API_FAIL=true
: > "$TEST_LOG"
expect_failure
if command grep -q 'helm upgrade' "$TEST_LOG"; then
  printf 'Helm upgraded despite API failure\n' >&2
  exit 1
fi
unset TEST_API_FAIL

export TEST_MOUNTED="" TEST_UPLOADS=1
mkdir "$TEST_DIR/uploads"
printf document > "$TEST_DIR/uploads/example.txt"
tar -cf "$TEST_DIR/uploads.tar" -C "$TEST_DIR/uploads" .
bash "$INSTALLER" --uploads-archive "$TEST_DIR/uploads.tar" > "$TEST_DIR/migrate.log"
command grep -q 'exec -i librechat-upload-restore' "$TEST_LOG"
export TEST_MOUNTED=uploads
expect_failure --uploads-archive "$TEST_DIR/uploads.tar"
command grep -q 'already mounted' "$TEST_DIR/failure.log"
printf 'PASS: RAG installer dry-run, prerequisites, stable credentials, namespace, rollout order, failures and upload migration\n'