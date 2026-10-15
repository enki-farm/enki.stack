RAG_DB_SECRET="librechat-rag-db-credentials"
RAG_IMAGE="registry.librechat.ai/librechat-ai/librechat-rag-api-dev-lite@sha256:a62ce2221b43a9bb57fa6cb733e7047c80132960272c33d5704028f3071a1251"
UPLOADS_ARCHIVE=""

render_rag() {
  local render_dir result
  render_dir="$(mktemp -d)"
  cp "$SCRIPT_DIR/rag/"*.yaml "$render_dir/"
  printf 'apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: %s\nresources:\n  - postgres.yaml\n  - api.yaml\n  - configmap.yaml\n  - uploads-pvc.yaml\npatches:\n  - target:\n      kind: Deployment\n      name: librechat-rag-api\n    patch: |-\n      - op: replace\n        path: /spec/template/spec/containers/0/image\n        value: %s\n' \
    "$NAMESPACE" "$RAG_IMAGE" > "$render_dir/kustomization.yaml"
  if kubectl kustomize "$render_dir"; then
    result=0
  else
    result=1
  fi
  rm -rf "$render_dir"
  return "$result"
}

rag_preflight() {
  local ready deployment mounted claim count archive_entry
  if [[ -n "$UPLOADS_ARCHIVE" ]]; then
    [[ -f "$UPLOADS_ARCHIVE" ]] || { err 'Uploads archive does not exist'; return 1; }
    tar -tf "$UPLOADS_ARCHIVE" >/dev/null || return 1
    while IFS= read -r archive_entry; do
      case "$archive_entry" in
        /*|../*|*/../*|*/..) err 'Uploads archive contains an unsafe path'; return 1 ;;
      esac
    done < <(tar -tf "$UPLOADS_ARCHIVE")
  fi
  ready="$(kubectl -n inference get inferenceservice bge-embeddings -o 'jsonpath={.status.conditions[?(@.type=="Ready")].status}')" || {
    err 'Install models/bge-embeddings first: kubectl apply -k models/bge-embeddings'
    return 1
  }
  [[ "$ready" == True ]] || { err 'BGE embedding model is not Ready'; return 1; }
  deployment="$(kubectl -n "$NAMESPACE" get deployment librechat-librechat --ignore-not-found -o jsonpath='{.metadata.name}')"
  [[ -n "$deployment" ]] || return 0
  mounted="$(kubectl -n "$NAMESPACE" get deployment librechat-librechat -o 'jsonpath={.spec.template.spec.containers[0].volumeMounts[?(@.mountPath=="/app/uploads")].name}')"
  claim="$(kubectl -n "$NAMESPACE" get deployment librechat-librechat -o 'jsonpath={.spec.template.spec.volumes[?(@.name=="uploads")].persistentVolumeClaim.claimName}')"
  if [[ "$mounted" == uploads && "$claim" == librechat-uploads ]]; then
    [[ -z "$UPLOADS_ARCHIVE" ]] || { err 'Uploads are already mounted; refusing to overwrite them from an archive'; return 1; }
    return 0
  fi
  count="$(kubectl -n "$NAMESPACE" exec deployment/librechat-librechat -- sh -c 'find /app/uploads -type f | wc -l')" || return 1
  count="${count//[[:space:]]/}"
  [[ "$count" =~ ^[0-9]+$ ]] || { err 'Cannot determine existing upload count'; return 1; }
  if [[ "$count" != 0 && -z "$UPLOADS_ARCHIVE" ]]; then
    err 'Existing uploads need migration. Back up /app/uploads and rerun with --uploads-archive <tar>; see the addon README.'
    return 1
  fi
}

ensure_rag_secret() {
  local requested existing pvc value
  requested="$(env_file_value RAG_POSTGRES_PASSWORD)"
  existing="$(kubectl -n "$NAMESPACE" get secret "$RAG_DB_SECRET" --ignore-not-found -o jsonpath='{.data.POSTGRES_PASSWORD}')"
  existing="$(printf '%s' "$existing" | base64 --decode)"
  pvc="$(kubectl -n "$NAMESPACE" get pvc data-librechat-rag-db-0 --ignore-not-found -o jsonpath='{.metadata.name}')"
  if [[ -n "$pvc" && -z "$existing" ]]; then
    err 'RAG database PVC exists without its credentials Secret; restore the original Secret before continuing'
    return 1
  fi
  if [[ -n "$existing" && -n "$requested" && "$requested" != "$existing" ]]; then
    err 'RAG database password differs from the existing Secret; rotate the PostgreSQL role explicitly, not via .env'
    return 1
  fi
  value="${existing:-$requested}"
  [[ -n "$value" ]] || value="$(openssl rand -hex 32)"
  kubectl -n "$NAMESPACE" create secret generic "$RAG_DB_SECRET" \
    "--from-literal=POSTGRES_PASSWORD=$value" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

restore_uploads() {
  [[ -n "$UPLOADS_ARCHIVE" ]] || return 0
  kubectl -n "$NAMESPACE" apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: librechat-upload-restore
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsUser: 1000
    runAsGroup: 2000
    fsGroup: 2000
  containers:
    - name: restore
      image: busybox:1.37.0
      command: [sh, -c, "tail -f /dev/null"]
      volumeMounts:
        - name: uploads
          mountPath: /uploads
  volumes:
    - name: uploads
      persistentVolumeClaim:
        claimName: librechat-uploads
EOF
  if ! kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/librechat-upload-restore --timeout=5m || \
     ! kubectl -n "$NAMESPACE" exec -i librechat-upload-restore -- tar -xf - -C /uploads < "$UPLOADS_ARCHIVE"; then
    kubectl -n "$NAMESPACE" delete pod librechat-upload-restore --ignore-not-found >/dev/null
    err 'Upload restore failed; LibreChat has not been upgraded'
    return 1
  fi
  kubectl -n "$NAMESPACE" delete pod librechat-upload-restore --wait=true >/dev/null
}

install_rag() {
  ensure_rag_secret
  render_rag | kubectl -n "$NAMESPACE" apply -f -
  kubectl -n "$NAMESPACE" rollout status statefulset/librechat-rag-db --timeout=5m
  kubectl -n "$NAMESPACE" exec statefulset/librechat-rag-db -- sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SELECT 1"' >/dev/null
  kubectl -n "$NAMESPACE" rollout status deployment/librechat-rag-api --timeout=10m
  kubectl -n "$NAMESPACE" rollout restart deployment/librechat-rag-api
  kubectl -n "$NAMESPACE" rollout status deployment/librechat-rag-api --timeout=10m
  kubectl -n "$NAMESPACE" exec deployment/librechat-rag-api -- python -c '
import json, math, os, urllib.request
request = urllib.request.Request(
    os.environ["RAG_OPENAI_BASEURL"].rstrip("/") + "/embeddings",
    data=json.dumps({"model": "embedding", "input": "Readiness check", "encoding_format": "float"}).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=60) as response:
    vector = json.load(response)["data"][0]["embedding"]
assert len(vector) == 384 and all(math.isfinite(value) for value in vector), "Invalid BGE embedding"
'
  restore_uploads
}