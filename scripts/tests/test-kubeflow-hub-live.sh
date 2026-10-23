#!/usr/bin/env bash
set -euo pipefail

CONTEXT="${1:-spark}"
KUBECTL=(kubectl --context "$CONTEXT")
for binary in kubectl jq; do command -v "$binary" >/dev/null; done
for deployment in zot model-registry-db model-registry-deployment model-catalog-server model-registry-ui controller-controller-manager; do
  "${KUBECTL[@]}" -n kubeflow rollout status "deployment/$deployment" --timeout=90s
done
"${KUBECTL[@]}" -n kubeflow rollout status statefulset/model-catalog-postgres --timeout=90s
"${KUBECTL[@]}" -n kubeflow exec deployment/model-registry-db -- /bin/bash -c \
  'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -h 127.0.0.1 -u "$MYSQL_USER_NAME" -D "$MYSQL_DATABASE" -Nse "SELECT 1"' |
  command grep -qx 1
"${KUBECTL[@]}" -n kubeflow exec statefulset/model-catalog-postgres -- sh -c \
  'psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}" -v ON_ERROR_STOP=1 -Atc "SELECT 1"' |
  command grep -qx 1
for claim in metadata-mysql model-catalog-postgres zot-data; do
  "${KUBECTL[@]}" -n kubeflow get pvc "$claim" -o json | jq -e '.status.phase == "Bound"' >/dev/null
done
for service in zot model-registry-service model-registry-ui-service model-catalog; do
  "${KUBECTL[@]}" -n kubeflow get endpointslice -l "kubernetes.io/service-name=$service" -o json |
    jq -e '[.items[].endpoints[]? | select(.conditions.ready == true)] | length > 0' >/dev/null
done
"${KUBECTL[@]}" -n kubeflow get deployment model-registry-ui -o json |
  jq -e '.spec.template.spec.containers | length == 1 and .[0].name == "model-registry-ui"' >/dev/null
"${KUBECTL[@]}" -n kubeflow get statefulset model-catalog-postgres -o json |
  jq -e '.spec.template.spec.volumes | any(.persistentVolumeClaim.claimName == "model-catalog-postgres")' >/dev/null
"${KUBECTL[@]}" get clusterstoragecontainer model-registry-storage-initializer -o json |
  jq -e '.spec.container.env | any(.name == "MODEL_REGISTRY_BASE_URL" and .value == "model-registry-service.kubeflow.svc.cluster.local:8080")' >/dev/null
printf '[PASS] Hub MySQL, catalog PostgreSQL, Zot, persistent storage and service endpoints in kubeflow\n'
printf '[NOTE] Run test-kubeflow-hub-auth-live.sh separately to verify gateway identity enforcement.\n'
printf '[NOTE] Run test-zot-hub-live.sh for a real Hub transfer job and private KServe OCI prediction.\n'
