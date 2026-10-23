#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM=""
CONTEXT=""
AUTH_DIR="$ROOT_DIR/.generated/auth"
ENV_FILE="$ROOT_DIR/k8s/kubeflow-hub/.env"
DRY_RUN=false
PUBLISH=false

fail() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform|--context|--auth-dir|--env-file)
      [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail "$1 requires a value"
      case "$1" in
        --platform) PLATFORM="$2" ;; --context) CONTEXT="$2" ;; --auth-dir) AUTH_DIR="$2" ;; --env-file) ENV_FILE="$2" ;;
      esac
      shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --publish) PUBLISH=true; shift ;;
    -h|--help)
      printf '%s\n' 'Usage: bash scripts/install-kubeflow-hub.sh --platform <dgx-spark|aks> [--context <name>] [--auth-dir <dir>] [--env-file <path>] [--dry-run] [--publish]'
      exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done
case "$PLATFORM" in dgx-spark) STORAGE_CLASS=local-path ;; aks) STORAGE_CLASS=managed-csi ;; *) fail '--platform must be dgx-spark or aks' ;; esac
for binary in kubectl openssl jq htpasswd; do command -v "$binary" >/dev/null || fail "$binary is required"; done
if command -v kustomize >/dev/null 2>&1; then
  RENDER_COMMAND=(kustomize build --load-restrictor=LoadRestrictionsNone)
else
  RENDER_COMMAND=(kubectl kustomize --load-restrictor=LoadRestrictionsNone)
fi
[[ -f "$AUTH_DIR/hub.env" ]] || fail 'Render Dex auth with HUB_URL and HUB_GROUP configured before installing core Hub'
command grep -qx 'HUB_URL=https://hub.zer0.garden' "$AUTH_DIR/hub.env" || fail 'Hub requires HUB_URL=https://hub.zer0.garden'
command grep -qx 'HUB_GROUP=enki-farm:core' "$AUTH_DIR/hub.env" || fail 'Hub requires HUB_GROUP=enki-farm:core'
umask 077
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
"${RENDER_COMMAND[@]}" "$ROOT_DIR/k8s/kubeflow-hub/overlays/$PLATFORM" > "$TEMP_DIR/hub.yaml"
"${RENDER_COMMAND[@]}" "$ROOT_DIR/k8s/kubeflow-hub/jobs" > "$TEMP_DIR/jobs.yaml"
[[ -s "$TEMP_DIR/hub.yaml" ]] || fail 'Hub render is empty'
if command grep -Eq 'image: .*:latest|user@example.com|name: cluster-admin|kind: Secret|MYSQL_ALLOW_EMPTY_PASSWORD' "$TEMP_DIR/hub.yaml"; then fail 'Unsafe Hub render'; fi
if [[ "$PUBLISH" == true ]]; then
  [[ -f "$AUTH_DIR/hub-route.yaml" ]] || fail 'Hub route was not rendered by the auth installer'
  if command grep -q 'namespace: kubeflow-hub' "$AUTH_DIR/hub-route.yaml"; then
    fail 'Hub route uses the old namespace; rerender auth with scripts/install-auth.sh --dry-run'
  fi
  if ! command grep -q 'type: URLRewrite' "$AUTH_DIR/hub-route.yaml" ||
     ! command grep -q 'replacePrefixMatch: /' "$AUTH_DIR/hub-route.yaml"; then
    fail "Hub route lacks the frontend asset rewrite; rerender auth with scripts/install-auth.sh --output-dir \"$AUTH_DIR\" --dry-run"
  fi
fi
if [[ "$DRY_RUN" == true ]]; then
  printf '[DRY-RUN] Hub, databases, Zot and transfer-job RBAC validated for %s; no cluster access or secrets generated\n' "$PLATFORM"
  exit 0
fi
[[ -n "$CONTEXT" ]] || CONTEXT="$(kubectl config current-context)"
KUBECTL=(kubectl --context "$CONTEXT")
"${KUBECTL[@]}" --request-timeout=15s get --raw=/readyz >/dev/null
"${KUBECTL[@]}" get storageclass "$STORAGE_CLASS" >/dev/null
"${KUBECTL[@]}" get crd clusterstoragecontainers.serving.kserve.io inferenceservices.serving.kserve.io >/dev/null
"${KUBECTL[@]}" get namespace inference >/dev/null
existing_initializer="$("${KUBECTL[@]}" get clusterstoragecontainer model-registry-storage-initializer --ignore-not-found -o 'jsonpath={.spec.container.env[?(@.name=="MODEL_REGISTRY_BASE_URL")].value}')"
case "$existing_initializer" in
  ""|model-registry-service.kubeflow.svc.cluster.local:8080) ;;
  model-registry-service.kubeflow-hub.svc.cluster.local:8080)
    printf '[INFO] Repointing this stack storage initializer from kubeflow-hub to kubeflow.\n' ;;
  *) fail 'A different registry owns the storage initializer; refusing takeover' ;;
esac
if [[ "$PUBLISH" == true ]]; then
  client_secret="$("${KUBECTL[@]}" -n dex get secret dex-secrets -o 'jsonpath={.data.HUB_CLIENT_SECRET}' | base64 -d)"
  [[ -n "$client_secret" ]] || fail 'Install the Hub Dex client before publishing Hub'
fi
"${KUBECTL[@]}" apply -f "$ROOT_DIR/k8s/kubeflow-hub/base/namespace.yaml"

secret_value() {
  local secret="$1" key="$2" claim="$3" found value existing_claim
  found="$("${KUBECTL[@]}" -n kubeflow get secret "$secret" --ignore-not-found -o name)" || return 1
  if [[ -n "$found" ]]; then
    value="$("${KUBECTL[@]}" -n kubeflow get secret "$secret" -o "jsonpath={.data.$key}" | base64 -d)" || return 1
    [[ -n "$value" ]] || fail "Existing Secret kubeflow/$secret has no $key; refusing rotation"
    printf '%s' "$value"
  else
    existing_claim="$("${KUBECTL[@]}" -n kubeflow get pvc "$claim" --ignore-not-found -o name)" || return 1
    [[ -z "$existing_claim" ]] || fail "PVC kubeflow/$claim exists without $secret; restore credentials instead of regenerating them"
    openssl rand -hex 32
  fi
}
check_database_user() {
  local secret="$1" key="$2" expected="$3" found user
  found="$("${KUBECTL[@]}" -n kubeflow get secret "$secret" --ignore-not-found -o name)" || return 1
  if [[ -n "$found" ]]; then
    user="$("${KUBECTL[@]}" -n kubeflow get secret "$secret" -o "jsonpath={.data.$key}" | base64 -d)" || return 1
    [[ "$user" == "$expected" ]] || fail "Existing $secret username differs from $expected; refusing to rewrite initialized database credentials"
  fi
}
apply_secret() {
  "${KUBECTL[@]}" -n kubeflow create secret generic "$1" --from-env-file=/dev/stdin --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null
}
env_file_value() {
  local key="$1" line value
  [[ -f "$ENV_FILE" ]] || return 0
  line="$(command grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1 || true)"
  value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}
password="$(secret_value model-registry-db-secrets MYSQL_ROOT_PASSWORD metadata-mysql)"
catalog_password="$(secret_value model-catalog-postgres POSTGRES_PASSWORD model-catalog-postgres)"
check_database_user model-registry-db-secrets MYSQL_USER_NAME root
check_database_user model-catalog-postgres POSTGRES_USER hub
printf 'MYSQL_USER_NAME=root\nMYSQL_ROOT_PASSWORD=%s\n' "$password" | apply_secret model-registry-db-secrets
printf 'POSTGRES_USER=hub\nPOSTGRES_PASSWORD=%s\n' "$catalog_password" | apply_secret model-catalog-postgres
if [[ "$PUBLISH" == true ]]; then
  printf 'client-secret=%s\n' "$client_secret" | apply_secret hub-oidc
fi
hf_api_key="$(env_file_value HF_API_KEY)"
if [[ -n "$hf_api_key" ]]; then
  printf 'HF_API_KEY=%s\n' "$hf_api_key" | apply_secret model-catalog-hf-api-key
fi
zot_push_password="$(secret_value zot-auth hub-password zot-data)"
zot_pull_password="$(secret_value zot-auth kserve-password zot-data)"
printf '%s' "$zot_push_password" > "$TEMP_DIR/hub-password"
printf '%s' "$zot_pull_password" > "$TEMP_DIR/kserve-password"
printf '%s\n' "$zot_push_password" | htpasswd -niB hub > "$TEMP_DIR/htpasswd"
printf '%s\n' "$zot_pull_password" | htpasswd -niB kserve >> "$TEMP_DIR/htpasswd"
"${KUBECTL[@]}" -n kubeflow create secret generic zot-auth \
  --from-file=hub-password="$TEMP_DIR/hub-password" --from-file=kserve-password="$TEMP_DIR/kserve-password" \
  --from-file=htpasswd="$TEMP_DIR/htpasswd" --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null
for account in hub kserve; do
  jq -n --rawfile password "$TEMP_DIR/$account-password" --arg user "$account" \
    '{auths:{"zot.zer0.garden":{auth:(($user + ":" + $password)|@base64)}}}' > "$TEMP_DIR/$account-dockerconfig.json"
done
"${KUBECTL[@]}" -n kubeflow apply -f "$TEMP_DIR/jobs.yaml"
"${KUBECTL[@]}" -n kubeflow create secret generic zot-push --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$TEMP_DIR/hub-dockerconfig.json" --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null
"${KUBECTL[@]}" -n default delete rolebinding hub-registry-reader hub-transfer-jobs hub-transfer-users --ignore-not-found
"${KUBECTL[@]}" -n default delete role hub-registry-reader hub-transfer-jobs hub-transfer-users --ignore-not-found
"${KUBECTL[@]}" -n default delete secret zot-push --ignore-not-found
for namespace in kubeflow inference; do
  "${KUBECTL[@]}" -n "$namespace" create secret generic zot-pull --type=kubernetes.io/dockerconfigjson \
    --from-file=.dockerconfigjson="$TEMP_DIR/kserve-dockerconfig.json" --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null
done
unset zot_push_password zot_pull_password
"${KUBECTL[@]}" apply -f "$ROOT_DIR/k8s/kubeflow-hub/zot/inference-puller.yaml"
"${KUBECTL[@]}" apply -f "$TEMP_DIR/hub.yaml"
"${KUBECTL[@]}" -n kubeflow rollout status deployment/zot --timeout=5m
"${KUBECTL[@]}" -n kubeflow wait httproute/zot --for='jsonpath={.status.parents[0].conditions[?(@.type=="Accepted")].status}=True' --timeout=90s
"${KUBECTL[@]}" -n kubeflow wait httproute/zot --for='jsonpath={.status.parents[0].conditions[?(@.type=="ResolvedRefs")].status}=True' --timeout=90s
"${KUBECTL[@]}" -n kubeflow rollout status deployment/model-registry-db --timeout=5m
"${KUBECTL[@]}" -n kubeflow rollout status statefulset/model-catalog-postgres --timeout=5m
"${KUBECTL[@]}" -n kubeflow rollout restart deployment/model-catalog-server
for deployment in model-registry-deployment model-catalog-server model-registry-ui controller-controller-manager; do
  "${KUBECTL[@]}" -n kubeflow rollout status "deployment/$deployment" --timeout=5m
done
if [[ "$PUBLISH" == true ]]; then
  "${KUBECTL[@]}" apply --dry-run=server -f "$AUTH_DIR/hub-route.yaml" >/dev/null
  "${KUBECTL[@]}" apply -f "$AUTH_DIR/hub-route.yaml"
  "${KUBECTL[@]}" -n kubeflow wait httproute/kubeflow-hub --for='jsonpath={.status.parents[0].conditions[?(@.type=="Accepted")].status}=True' --timeout=90s
  "${KUBECTL[@]}" -n kubeflow wait httproute/kubeflow-hub --for='jsonpath={.status.parents[0].conditions[?(@.type=="ResolvedRefs")].status}=True' --timeout=90s
  "${KUBECTL[@]}" -n kubeflow wait securitypolicy/hub-login --for='jsonpath={.status.ancestors[0].conditions[?(@.type=="Accepted")].status}=True' --timeout=90s
  printf '[INFO] Hub route configured at https://hub.zer0.garden; verify a real Dex browser login.\n'
else
  printf '[INFO] Hub services and authenticated Zot installed on %s; --publish enables the Dex-protected Hub route.\n' "$CONTEXT"
fi
printf '[INFO] Zot: https://zot.zer0.garden; Hub destination zot.zer0.garden/models/<model>:<version>, user hub. Credentials are in kubeflow/zot-auth; KServe uses serviceAccountName: zot-model-puller and oci:// URLs.\n'
