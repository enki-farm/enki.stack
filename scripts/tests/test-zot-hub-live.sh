#!/usr/bin/env bash
set -euo pipefail

CONTEXT="${1:-spark}"
JOB_NAMESPACE="${2:-kubeflow}"
case "$JOB_NAMESPACE" in kubeflow) ;; *) printf 'Job namespace must be kubeflow\n' >&2; exit 1 ;; esac
KUBECTL=(kubectl --context "$CONTEXT")
NAME="zot-hub-check-$(date +%s)"
TEMP_DIR="$(mktemp -d)"
FORWARDS=()
MODEL_ID=""
umask 077
cleanup() {
  if [[ -n "$MODEL_ID" ]]; then
    curl --fail --silent --show-error --max-time 10 -X PATCH -H 'Content-Type: application/json' \
      -d '{"state":"ARCHIVED"}' "http://127.0.0.1:18084/api/model_registry/v1alpha3/registered_models/$MODEL_ID" >/dev/null ||
      printf '[WARN] Could not archive test model %s\n' "$MODEL_ID" >&2
  fi
  "${KUBECTL[@]}" -n inference delete inferenceservice,pod -l "enki.farm/zot-test=$NAME" --ignore-not-found --timeout=90s >/dev/null ||
    printf '[WARN] Could not remove inference test resources\n' >&2
  "${KUBECTL[@]}" -n "$JOB_NAMESPACE" delete job "$NAME" --ignore-not-found --timeout=90s >/dev/null ||
    printf '[WARN] Could not remove transfer test job\n' >&2
  "${KUBECTL[@]}" -n kubeflow delete deployment,service -l "enki.farm/zot-test=$NAME" --ignore-not-found --timeout=90s >/dev/null ||
    printf '[WARN] Could not remove model source\n' >&2
  for process in ${FORWARDS[@]+"${FORWARDS[@]}"}; do kill "$process" 2>/dev/null || true; done
  for process in ${FORWARDS[@]+"${FORWARDS[@]}"}; do wait "$process" 2>/dev/null || true; done
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT
for binary in kubectl curl jq; do command -v "$binary" >/dev/null; done
for account in hub kserve; do
  "${KUBECTL[@]}" -n kubeflow get secret zot-auth -o json |
    jq -jr --arg key "$account-password" '.data[$key]|@base64d' > "$TEMP_DIR/$account-password"
  jq -nr --arg user "$account" --rawfile password "$TEMP_DIR/$account-password" \
    '"user = \"" + $user + ":" + $password + "\""' > "$TEMP_DIR/$account-curl.config"
done
anonymous="$(curl --silent --show-error --max-time 15 -o /dev/null -w '%{http_code}' https://zot.zer0.garden/v2/)"
[[ "$anonymous" == 401 ]]
curl --config "$TEMP_DIR/hub-curl.config" --fail --silent --show-error --max-time 15 https://zot.zer0.garden/v2/ >/dev/null
curl --config "$TEMP_DIR/kserve-curl.config" --fail --silent --show-error --max-time 15 https://zot.zer0.garden/v2/ >/dev/null
denied="$(curl --config "$TEMP_DIR/kserve-curl.config" --silent --show-error --max-time 15 -X POST \
  -o /dev/null -w '%{http_code}' "https://zot.zer0.garden/v2/models/$NAME/blobs/uploads/")"
[[ "$denied" == 403 ]]
printf '[PASS] Zot requires authentication and KServe account cannot push\n'

source_code='import os, joblib, http.server
from sklearn.datasets import load_iris
from sklearn.tree import DecisionTreeClassifier
features, targets = load_iris(return_X_y=True)
os.chdir("/tmp")
joblib.dump(DecisionTreeClassifier(random_state=42).fit(features, targets), "model.joblib")
http.server.HTTPServer(("0.0.0.0", 8080), http.server.SimpleHTTPRequestHandler).serve_forever()'
jq -n --arg name "$NAME" --arg code "$source_code" '{
  apiVersion:"apps/v1",kind:"Deployment",metadata:{name:$name,namespace:"kubeflow",labels:{"enki.farm/zot-test":$name}},
  spec:{replicas:1,selector:{matchLabels:{"enki.farm/zot-test":$name}},template:{
    metadata:{labels:{"enki.farm/zot-test":$name}},spec:{automountServiceAccountToken:false,
      containers:[{name:"source",image:"kserve/sklearnserver:v0.20.0",command:["python","-c",$code],
        ports:[{containerPort:8080}],readinessProbe:{httpGet:{path:"/model.joblib",port:8080}},
        resources:{requests:{cpu:"100m",memory:"256Mi"},limits:{memory:"512Mi"}}}]}}}}' |
  "${KUBECTL[@]}" apply -f - >/dev/null
jq -n --arg name "$NAME" '{apiVersion:"v1",kind:"Service",metadata:{name:$name,namespace:"kubeflow",labels:{"enki.farm/zot-test":$name}},
  spec:{selector:{"enki.farm/zot-test":$name},ports:[{port:8080,targetPort:8080}]}}' |
  "${KUBECTL[@]}" apply -f - >/dev/null
"${KUBECTL[@]}" -n kubeflow rollout status "deployment/$NAME" --timeout=180s
"${KUBECTL[@]}" -n kubeflow port-forward service/model-registry-ui-service 18083:8080 > "$TEMP_DIR/ui-forward.log" 2>&1 &
FORWARDS+=("$!")
"${KUBECTL[@]}" -n kubeflow port-forward service/model-registry-service 18084:8080 > "$TEMP_DIR/registry-forward.log" 2>&1 &
FORWARDS+=("$!")
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:18083/healthcheck >/dev/null
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:18084/readyz/health >/dev/null
jq -n --arg name "$NAME" --arg namespace "$JOB_NAMESPACE" --rawfile password "$TEMP_DIR/hub-password" '{data:{
  name:$name,jobDisplayName:$name,namespace:$namespace,uploadIntent:"create_model",
  registeredModelName:$name,modelVersionName:"v1",modelArtifactName:"model",
  sourceModelFormat:"sklearn",sourceModelFormatVersion:"1",
  source:{type:"uri",uri:("http://"+$name+".kubeflow.svc.cluster.local:8080/model.joblib")},
  destination:{type:"oci",registry:"zot.zer0.garden",uri:("zot.zer0.garden/models/"+$name+":v1"),username:"hub",password:$password}
}}' > "$TEMP_DIR/job-request.json"
curl --fail-with-body --silent --show-error --max-time 60 -H 'Content-Type: application/json' \
  -H 'kubeflow-userid: zot-test@example.invalid' -H 'kubeflow-groups: enki-farm:core' \
  -d @"$TEMP_DIR/job-request.json" \
  'http://127.0.0.1:18083/api/v1/model_registry/model-registry-service/model_transfer_jobs?namespace=kubeflow' > "$TEMP_DIR/job-response.json"
if ! "${KUBECTL[@]}" -n "$JOB_NAMESPACE" wait "job/$NAME" --for=condition=Complete --timeout=600s; then
  "${KUBECTL[@]}" -n "$JOB_NAMESPACE" get pods -l "job-name=$NAME"
  "${KUBECTL[@]}" -n "$JOB_NAMESPACE" logs "job/$NAME" --tail=30
  exit 1
fi
curl --fail --silent --show-error --max-time 15 'http://127.0.0.1:18084/api/model_registry/v1alpha3/registered_models?pageSize=1000' > "$TEMP_DIR/models.json"
MODEL_ID="$(jq -er --arg name "$NAME" '.items[] | select(.name==$name) | .id' "$TEMP_DIR/models.json")"
printf '[PASS] Hub created a transfer job, pushed the model to Zot and registered model %s\n' "$MODEL_ID"

curl --config "$TEMP_DIR/kserve-curl.config" --fail --silent --show-error --max-time 30 \
  -H 'Accept: application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
  -D "$TEMP_DIR/manifest-headers" "https://zot.zer0.garden/v2/models/$NAME/manifests/v1" > "$TEMP_DIR/manifest.json"
digest="$(sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: *//p' "$TEMP_DIR/manifest-headers" | tr -d '\r')"
[[ "$digest" == sha256:* ]]
if command grep -Eiq '^cf-cache-status: HIT' "$TEMP_DIR/manifest-headers"; then
  printf 'Authenticated registry manifest was cached by Cloudflare\n' >&2
  exit 1
fi
jq -n --arg name "$NAME" --arg uri "oci://zot.zer0.garden/models/$NAME@$digest" '{
  apiVersion:"serving.kserve.io/v1beta1",kind:"InferenceService",
  metadata:{name:$name,namespace:"inference",labels:{"enki.farm/zot-test":$name},annotations:{"serving.kserve.io/deploymentMode":"Standard"}},
  spec:{predictor:{serviceAccountName:"zot-model-puller",model:{modelFormat:{name:"sklearn"},runtime:"kserve-sklearnserver",storageUri:$uri,
    resources:{requests:{cpu:"100m",memory:"256Mi"},limits:{memory:"1Gi"}}}}}}' |
  "${KUBECTL[@]}" apply -f - >/dev/null
"${KUBECTL[@]}" -n inference wait "inferenceservice/$NAME" --for=condition=Ready --timeout=300s
"${KUBECTL[@]}" -n inference run "$NAME-request" --image=curlimages/curl:8.16.0 --restart=Never \
  --labels="enki.farm/zot-test=$NAME" --command -- curl --fail --silent --show-error --retry 10 --retry-all-errors --retry-delay 1 \
  --connect-timeout 3 --max-time 10 -H 'Content-Type: application/json' -d '{"instances":[[6.8,2.8,4.8,1.4]]}' \
  "http://$NAME-predictor.inference.svc.cluster.local/v1/models/$NAME:predict" >/dev/null
"${KUBECTL[@]}" -n inference wait "pod/$NAME-request" --for=jsonpath='{.status.phase}'=Succeeded --timeout=120s
"${KUBECTL[@]}" -n inference logs "$NAME-request" | jq -e '.predictions == [1]' >/dev/null
printf '[PASS] KServe node pulled the private OCI model by digest and predicted [1]\n'
printf '[NOTE] Test registry metadata is archived on exit. The small models/%s OCI repository is retained; no production artifacts are deleted.\n' "$NAME"
