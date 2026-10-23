#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT

for platform in dgx-spark aks; do
  kustomize build --load-restrictor=LoadRestrictionsNone "$ROOT_DIR/k8s/kubeflow-hub/overlays/$platform" > "$TEST_DIR/$platform.yaml"
  if command grep -Eq 'image: .*:latest|user@example.com|name: cluster-admin|MYSQL_ALLOW_EMPTY_PASSWORD|kind: Secret|namespace: (kubeflow-hub|model-catalog)' "$TEST_DIR/$platform.yaml"; then
    printf 'Unsafe resource in %s render\n' "$platform" >&2
    exit 1
  fi
  ruby -ryaml -e '
    docs = YAML.load_stream(File.read(ARGV[0]))
    find = ->(kind, name) { docs.find { |d| d["kind"] == kind && d["metadata"]["name"] == name } || abort("Missing #{kind}/#{name}") }
    docs.each do |d|
      ns = d["metadata"]["namespace"]
      abort("Unexpected namespace #{ns}") if ns && ns != "kubeflow"
      (d["subjects"] || []).select { |s| s["kind"] == "ServiceAccount" }.each do |s|
        abort("Wrong ServiceAccount namespace") unless s["namespace"] == "kubeflow"
      end
    end
    ui = find.call("Deployment", "model-registry-ui")["spec"]["template"]["spec"]
    abort("Demo proxy present") unless ui["containers"].length == 1
    abort("Wrong UI mode") unless ui["containers"][0]["args"].include?("--deployment-mode=standalone")
    abort("Wrong compiled frontend mode") unless ui["containers"][0]["image"] == "ghcr.io/kubeflow/hub/ui-standalone:v0.3.17"
    service = find.call("Service", "model-registry-ui-service")
    abort("Wrong backend ports") unless service["spec"]["ports"][0].values_at("port", "targetPort") == [8080, 8080]
    db = find.call("Deployment", "model-registry-db")["spec"]["template"]["spec"]["containers"][0]
    abort("Not MySQL") unless db["image"].end_with?("mysql:8.3")
    probe = db["readinessProbe"]["exec"]["command"].last
    abort("Broken MySQL probe expansion") if probe.include?("$$")
    catalog = find.call("StatefulSet", "model-catalog-postgres")["spec"]["template"]["spec"]
    abort("Wrong PostgreSQL UID/image") unless catalog["containers"][0]["image"] == "postgres:17.6-alpine"
    volume = catalog["volumes"].find { |v| v["name"] == "model-catalog-postgres" }
    abort("Ephemeral catalog database") if volume.key?("emptyDir")
    abort("Wrong catalog PVC") unless volume["persistentVolumeClaim"]["claimName"] == "model-catalog-postgres"
    %w[metadata-mysql model-catalog-postgres zot-data].each do |name|
      pvc = find.call("PersistentVolumeClaim", name)
      abort("Wrong storage class") unless pvc["spec"]["storageClassName"] == ARGV[1]
    end
    initializer = find.call("ClusterStorageContainer", "model-registry-storage-initializer")
    url = initializer["spec"]["container"]["env"].find { |e| e["name"] == "MODEL_REGISTRY_BASE_URL" }["value"]
    abort("Wrong initializer URL") unless url == "model-registry-service.kubeflow.svc.cluster.local:8080"
    abort("Catalog discovery label missing") unless find.call("Service", "model-catalog")["metadata"]["labels"]["component"] == "model-catalog"
    binding = find.call("RoleBinding", "hub-reader")
    abort("Missing Dex group access") unless binding["subjects"].any? { |s| s["kind"] == "Group" && s["name"] == "enki-farm:core" }
    role = find.call("Role", "hub-job-registry-reader")
    abort("Overbroad job registry permissions") unless role["rules"] == [{"apiGroups"=>[""], "resources"=>["services"], "resourceNames"=>["model-registry-service"], "verbs"=>["get"]}]
    subjects = find.call("RoleBinding", "hub-job-registry-reader")["subjects"]
    abort("Wrong job registry subjects") unless subjects == [{"kind"=>"User", "name"=>"system:serviceaccount:kubeflow:default", "apiGroup"=>"rbac.authorization.k8s.io"}]
    zot = find.call("Deployment", "zot")["spec"]["template"]["spec"]
    abort("Wrong Zot image") unless zot["containers"][0]["image"] == "ghcr.io/project-zot/zot:v2.1.22"
    abort("Unprotected Zot credentials") unless zot["volumes"].any? { |v| v.dig("secret", "secretName") == "zot-auth" }
    abort("Missing Zot persistence") unless zot["volumes"].any? { |v| v.dig("persistentVolumeClaim", "claimName") == "zot-data" }
    abort("Unexpected Zot exposure") unless find.call("Service", "zot")["spec"].fetch("type", "ClusterIP") == "ClusterIP"
    route = find.call("HTTPRoute", "zot")
    abort("Wrong Zot hostname") unless route["spec"]["hostnames"] == ["zot.zer0.garden"]
    abort("Missing node pull credentials") unless find.call("ServiceAccount", "zot-model-puller")["imagePullSecrets"] == [{"name"=>"zot-pull"}]
  ' "$TEST_DIR/$platform.yaml" "$(if [[ "$platform" == aks ]]; then printf managed-csi; else printf local-path; fi)"
done
jq -e '.http.auth.htpasswd.path == "/auth/htpasswd" and
  (.http.accessControl.repositories["models/**"].policies | length == 2) and
  (.http.accessControl.repositories["models/**"].policies[] | select(.users == ["kserve"]) | .actions == ["read"])' \
  "$ROOT_DIR/k8s/kubeflow-hub/zot/config.json" >/dev/null
kustomize build "$ROOT_DIR/k8s/kubeflow-hub/jobs" > "$TEST_DIR/jobs.yaml"
if command grep -q 'namespace:' "$TEST_DIR/jobs.yaml"; then
  printf 'Job RBAC must support explicit target namespaces\n' >&2
  exit 1
fi
ruby -ryaml -e '
  docs = YAML.load_stream(File.read(ARGV[0]))
  abort("Unexpected job RBAC scope") unless docs.all? { |d| %w[Role RoleBinding].include?(d["kind"]) }
  binding = docs.find { |d| d["kind"] == "RoleBinding" && d["metadata"]["name"] == "hub-transfer-jobs" }
  abort("Wrong job executor") unless binding["subjects"] == [{"apiGroup"=>"rbac.authorization.k8s.io", "kind"=>"User", "name"=>"system:serviceaccount:kubeflow:model-registry-ui"}]
' "$TEST_DIR/jobs.yaml"

ruby -ryaml -e '
  route = YAML.load(File.read(ARGV[0]).split(/^---$/).last.gsub(/@[A-Z_]+@/, "example"))
  rule = route["spec"]["rules"].find { |r| r["matches"].any? { |m| m.dig("path", "value") == "/model-registry" } }
  abort("Missing frontend prefix route") unless rule
  rewrite = rule["filters"].find { |f| f["type"] == "URLRewrite" }
  abort("Missing frontend prefix rewrite") unless rewrite&.dig("urlRewrite", "path") == {"type" => "ReplacePrefixMatch", "replacePrefixMatch" => "/"}
  route["spec"]["rules"].each do |r|
    cache = r["filters"].find { |f| f["type"] == "ResponseHeaderModifier" }
    abort("Authenticated responses may be cached") unless cache&.dig("responseHeaderModifier", "set")&.any? { |h| h["name"] == "Cache-Control" && h["value"] == "private, no-store" }
    abort("Wrong UI backend") unless r["backendRefs"].any? { |b| b["name"] == "model-registry-ui-service" && b["port"] == 8080 }
    headers = r["filters"].find { |f| f["type"] == "RequestHeaderModifier" }
    abort("Missing authenticated group header") unless headers.dig("requestHeaderModifier", "set").any? { |h| h["name"] == "kubeflow-groups" && h["value"] == "enki-farm:core" }
  end
' "$ROOT_DIR/k8s/dex/templates/hub-route.yaml"

if rg -n '^\s*-?\s*https?://' "$ROOT_DIR/k8s/kubeflow-hub" -g kustomization.yaml; then
  printf 'Remote Kustomize resources are not permitted\n' >&2
  exit 1
fi
mkdir -p "$TEST_DIR/bin" "$TEST_DIR/auth" "$TEST_DIR/payloads"
ln -s "$ROOT_DIR/scripts/tests/hub-kubectl-stub.sh" "$TEST_DIR/bin/kubectl"
export PATH="$TEST_DIR/bin:$PATH"
export HUB_TEST_LOG="$TEST_DIR/kubectl.log"
export HUB_TEST_HF_PAYLOAD="$TEST_DIR/hf-payload"
export HUB_TEST_PAYLOAD_DIR="$TEST_DIR/payloads"
printf 'HUB_URL=https://hub.zer0.garden\nHUB_GROUP=enki-farm:core\n' > "$TEST_DIR/auth/hub.env"
cp "$ROOT_DIR/k8s/dex/templates/hub-route.yaml" "$TEST_DIR/auth/hub-route.yaml"
printf 'HF_API_KEY="hf-test-token"\nIGNORED=$(touch %s)\n' "$TEST_DIR/executed" > "$TEST_DIR/hub.env"
install_hub() {
  bash "$ROOT_DIR/scripts/install-kubeflow-hub.sh" --platform dgx-spark --context isolated-test --auth-dir "$TEST_DIR/auth" --env-file "$TEST_DIR/hub.env" "$@" > "$TEST_DIR/output" 2>&1
}
: > "$HUB_TEST_LOG"
install_hub --dry-run --publish
[[ ! -s "$HUB_TEST_LOG" && ! -e "$HUB_TEST_HF_PAYLOAD" ]]

install_hub
command grep -qx 'MYSQL_USER_NAME=root' "$TEST_DIR/payloads/model-registry-db-secrets"
command grep -Eq '^MYSQL_ROOT_PASSWORD=[0-9a-f]{64}$' "$TEST_DIR/payloads/model-registry-db-secrets"
command grep -qx 'POSTGRES_USER=hub' "$TEST_DIR/payloads/model-catalog-postgres"
command grep -Eq '^POSTGRES_PASSWORD=[0-9a-f]{64}$' "$TEST_DIR/payloads/model-catalog-postgres"

export HUB_TEST_EXISTING=true
install_hub
command grep -qx 'MYSQL_ROOT_PASSWORD=preserved-test-value' "$TEST_DIR/payloads/model-registry-db-secrets"
command grep -qx 'POSTGRES_PASSWORD=preserved-test-value' "$TEST_DIR/payloads/model-catalog-postgres"
jq -e '.auths["zot.zer0.garden"].auth == ("hub:preserved-test-value"|@base64)' "$TEST_DIR/payloads/zot-push.json" >/dev/null
jq -e '.auths["zot.zer0.garden"].auth == ("kserve:preserved-test-value"|@base64)' "$TEST_DIR/payloads/zot-pull.json" >/dev/null
if command grep -q -- '-n default apply -f .*jobs.yaml' "$HUB_TEST_LOG"; then exit 1; fi
command grep -q -- '-n default delete rolebinding hub-registry-reader hub-transfer-jobs hub-transfer-users --ignore-not-found' "$HUB_TEST_LOG"
command grep -q -- '-n kubeflow apply -f .*jobs.yaml' "$HUB_TEST_LOG"
command grep -q -- '-n inference create secret generic zot-pull' "$HUB_TEST_LOG"
command grep -q -- '-n kubeflow rollout status deployment/zot' "$HUB_TEST_LOG"
command grep -qx 'HF_API_KEY=hf-test-token' "$HUB_TEST_HF_PAYLOAD"
[[ ! -e "$TEST_DIR/executed" ]]
if command grep -Eq 'hf-test-token|preserved-test-value' "$TEST_DIR/output" "$HUB_TEST_LOG"; then exit 1; fi
command grep -q -- '-n kubeflow rollout status deployment/model-registry-ui' "$HUB_TEST_LOG"
command grep -q -- '-n kubeflow rollout status statefulset/model-catalog-postgres' "$HUB_TEST_LOG"
command grep -q -- '-n kubeflow rollout restart deployment/model-catalog-server' "$HUB_TEST_LOG"
database_ready="$(command grep -n 'rollout status statefulset/model-catalog-postgres' "$HUB_TEST_LOG" | tail -n 1 | cut -d: -f1)"
catalog_restart="$(command grep -n 'rollout restart deployment/model-catalog-server' "$HUB_TEST_LOG" | tail -n 1 | cut -d: -f1)"
catalog_ready="$(command grep -n 'rollout status deployment/model-catalog-server' "$HUB_TEST_LOG" | tail -n 1 | cut -d: -f1)"
[[ "$database_ready" -lt "$catalog_restart" && "$catalog_restart" -lt "$catalog_ready" ]]
if command grep -Eq 'seaweedfs|psql|createdb|-n kubeflow-hub' "$HUB_TEST_LOG"; then exit 1; fi

: > "$HUB_TEST_LOG"
install_hub --env-file "$TEST_DIR/missing.env"
if command grep -q 'create secret generic model-catalog-hf-api-key' "$HUB_TEST_LOG"; then exit 1; fi

: > "$HUB_TEST_LOG"
export HUB_TEST_INITIALIZER=model-registry-service.kubeflow-hub.svc.cluster.local:8080
install_hub --publish
unset HUB_TEST_INITIALIZER
command grep -q 'create secret generic hub-oidc' "$HUB_TEST_LOG"
command grep -q 'ResolvedRefs' "$HUB_TEST_LOG"
command grep -q -- '-n kubeflow wait securitypolicy/hub-login' "$HUB_TEST_LOG"

for failure in HUB_TEST_SECRET_ERROR HUB_TEST_CONFLICT HUB_TEST_ROLLOUT_ERROR HUB_TEST_ROUTE_ERROR; do
  : > "$HUB_TEST_LOG"
  export "$failure=true"
  if install_hub --publish; then
    printf 'Expected installer failure for %s\n' "$failure" >&2
    exit 1
  fi
  case "$failure" in
    HUB_TEST_SECRET_ERROR|HUB_TEST_CONFLICT)
      if command grep -q 'create secret' "$HUB_TEST_LOG"; then exit 1; fi ;;
    HUB_TEST_ROLLOUT_ERROR)
      if command grep -q 'apply --dry-run=server' "$HUB_TEST_LOG"; then exit 1; fi ;;
  esac
  unset "$failure"
done
unset HUB_TEST_EXISTING
export HUB_TEST_DATA_EXISTS=true
: > "$HUB_TEST_LOG"
if install_hub; then
  printf 'Expected refusal to regenerate credentials for existing data\n' >&2
  exit 1
fi
command grep -q 'restore credentials' "$TEST_DIR/output"
if command grep -q 'create secret' "$HUB_TEST_LOG"; then exit 1; fi
unset HUB_TEST_DATA_EXISTS
export HUB_TEST_EXISTING=true HUB_TEST_ZOT_SECRET_MISSING=true HUB_TEST_ZOT_DATA_EXISTS=true
: > "$HUB_TEST_LOG"
if install_hub; then exit 1; fi
command grep -q 'PVC kubeflow/zot-data exists without zot-auth; restore credentials' "$TEST_DIR/output"
if command grep -q 'create secret generic zot-auth' "$HUB_TEST_LOG"; then exit 1; fi
unset HUB_TEST_ZOT_SECRET_MISSING HUB_TEST_ZOT_DATA_EXISTS
export HUB_TEST_ZOT_KEY_MISSING=true
: > "$HUB_TEST_LOG"
if install_hub; then exit 1; fi
command grep -q 'Existing Secret kubeflow/zot-auth has no hub-password; refusing rotation' "$TEST_DIR/output"
if command grep -q 'create secret generic zot-auth' "$HUB_TEST_LOG"; then exit 1; fi
unset HUB_TEST_EXISTING HUB_TEST_ZOT_KEY_MISSING
sed '/type: URLRewrite/d' "$ROOT_DIR/k8s/dex/templates/hub-route.yaml" > "$TEST_DIR/auth/hub-route.yaml"
: > "$HUB_TEST_LOG"
if install_hub --publish; then exit 1; fi
command grep -q 'lacks the frontend asset rewrite' "$TEST_DIR/output"
[[ ! -s "$HUB_TEST_LOG" ]]
cp "$ROOT_DIR/k8s/dex/templates/hub-route.yaml" "$TEST_DIR/auth/hub-route.yaml"
sed 's/namespace: kubeflow$/namespace: kubeflow-hub/' "$TEST_DIR/auth/hub-route.yaml" > "$TEST_DIR/old-route.yaml"
mv "$TEST_DIR/old-route.yaml" "$TEST_DIR/auth/hub-route.yaml"
: > "$HUB_TEST_LOG"
if install_hub --publish; then exit 1; fi
command grep -q 'rerender auth' "$TEST_DIR/output"
[[ ! -s "$HUB_TEST_LOG" ]]
printf 'PASS: Hub MySQL/catalog renders, credentials, namespace and authenticated route publication\n'
