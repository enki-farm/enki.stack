# Agent Router v1.1.0 + NVIDIA NeMo Guardrails Integration Plan

## Goal

Integrate NVIDIA NeMo Guardrails into the existing Kubernetes deployment:

* Agent Router v1.1.0
* Envoy Gateway 1.8.1
* Envoy 1.38.1
* Gateway API
* No custom guardrail proxy

Use NVIDIA's existing:

```text
nvcr.io/nvidia/nemo-microservices/guardrails-callout
```

as an Envoy External Processor (`ext_proc`).

The desired architecture is:

```text
Client
  │
  ▼
Envoy Gateway / Agent Router
  │
  ├── generic envoy.filters.http.ext_proc
  │       │
  │       └── NVIDIA guardrails-callout
  │
  ├── envoy.filters.http.buffer
  │
  ├── envoy.filters.http.ext_proc/aigateway
  │       │
  │       └── Agent Router AI Gateway extproc
  │
  ▼
LLM backend
```

For responses, Envoy processes HTTP filters in the reverse direction, so NeMo should see the response after Agent Router's processing.

Do **not** build a custom `guardrail-proxy`.

---

# 1. Existing environment

Current Agent Router data-plane pod:

```text
envoy-envoy-ai-gateway-system-envoy-ai-gateway-basic-2e891l2g7s
```

Namespace:

```text
envoy-gateway-system
```

Gateway:

```text
envoy-ai-gateway-system/envoy-ai-gateway-basic
```

Agent Router version:

```text
v1.1.0
```

Envoy Gateway:

```text
v1.8.1
```

Envoy:

```text
v1.38.1
```

The Envoy data-plane pod currently contains the Agent Router extproc:

```text
envoy.filters.http.ext_proc/aigateway
```

which communicates with:

```text
ai-gateway-extproc-uds
```

over the Unix domain socket.

---

# 2. Important discovery from Agent Router v1.1.0

Agent Router PR #963 changed the architecture from `EnvoyExtensionPolicy`-managed AI Gateway extproc to extension-server/xDS injection.

PR:

```text
https://github.com/theagentrouter/agent-router/pull/963
```

The v1.1.0 implementation creates:

```text
envoy.filters.http.ext_proc/aigateway
```

and injects it into Envoy's HTTP filter chain.

The important implementation detail is that Agent Router deliberately inserts its extproc around the Envoy buffer filter.

The Agent Router filter name is:

```text
envoy.filters.http.ext_proc/aigateway
```

The generic Envoy Gateway extproc name is:

```text
envoy.filters.http.ext_proc
```

The Envoy Gateway `EnvoyProxy.spec.filterOrder` CRD does **not** allow `/aigateway` as a filter name.

The live CRD allows:

```text
envoy.filters.http.ext_proc
```

but not:

```text
envoy.filters.http.ext_proc/aigateway
```

Therefore, do NOT attempt:

```yaml
filterOrder:
  - name: envoy.filters.http.ext_proc
    before: envoy.filters.http.ext_proc/aigateway
```

It will not validate.

---

# 3. Supported composition mechanism

Use Envoy Gateway's generic filter ordering:

```yaml
spec:
  filterOrder:
    - name: envoy.filters.http.ext_proc
      before: envoy.filters.http.buffer
```

This intentionally puts the user-provided NeMo extproc before the Envoy buffer filter.

Agent Router's v1.1.0 extension-server logic then inserts:

```text
envoy.filters.http.ext_proc/aigateway
```

after the buffer position.

The intended resulting order is:

```text
envoy.filters.http.ext_proc
envoy.filters.http.buffer
envoy.filters.http.ext_proc/aigateway
...
envoy.filters.http.router
```

This is the important integration mechanism.

---

# 4. Desired request/response behavior

Request:

```text
Client
  │
  ▼
NeMo guardrails-callout
  │
  ▼
Envoy buffer
  │
  ▼
Agent Router ext_proc/aigateway
  │
  ▼
LLM
```

Response:

```text
LLM
  │
  ▼
Agent Router ext_proc/aigateway
  │
  ▼
NeMo guardrails-callout
  │
  ▼
Client
```

This lets NeMo inspect the client-facing request before provider-specific AI Gateway transformation and inspect the response after Agent Router processing.

---

# 5. NVIDIA guardrails-callout

Use NVIDIA's existing image:

```text
nvcr.io/nvidia/nemo-microservices/guardrails-callout
```

Do not implement another extproc server.

NVIDIA's guardrails-callout is specifically designed to operate as an Envoy External Processor.

NVIDIA also supports model-to-guardrail configuration mapping, for example:

```yaml
guardrails:
  default_refusal_text: "I'm sorry, I can't respond to that."
  models:
    fake-model:
      config_ids:
        - default/nemoguard
```

NVIDIA's callout also supports streaming checks using:

```text
GR_EXTPROC__EVENTS_PER_CHECK
```

For the initial implementation, prefer buffered request/response processing for simplicity and compatibility with the existing Agent Router extproc.

Do not optimize for full-duplex streaming in the first implementation.

---

# 6. Deploy the NVIDIA callout

Use NVIDIA's NeMo Guardrails Helm deployment with the external processor enabled.

The relevant configuration concept is:

```yaml
guardrailsExtProc:
  enabled: true

  extProcImage:
    repository: nvcr.io/nvidia/nemo-microservices/guardrails-callout
    tag: ""

  env:
    GR_EXTPROC__EVENTS_PER_CHECK: 200

  configFile:
    data:
      guardrails:
        default_refusal_text: "I'm sorry, I can't respond to that."
        models:
          fake-model:
            config_ids:
              - default/nemoguard
```

The exact NVIDIA chart/version and generated Service name/port must be verified from the installed NVIDIA chart rather than hard-coded.

Inspect the rendered resources:

```bash
helm template <release> <chart> \
  --namespace <nemo-namespace> \
  -f values.yaml
```

Then identify:

```bash
kubectl get svc -n <nemo-namespace>
kubectl get pods -n <nemo-namespace>
kubectl get endpoints -n <nemo-namespace>
```

The Envoy `EnvoyExtensionPolicy` must point to the **gRPC guardrails-callout Service**, not the normal NeMo Guardrails HTTP API.

Do not assume port `7331` is the extproc port.

Verify the actual callout Service and port from the rendered chart.

---

# 7. Namespace recommendation

Initially deploy the NeMo guardrails components into:

```text
envoy-ai-gateway-system
```

or otherwise make sure the Envoy Gateway data plane can legally reference the Service.

Keeping the callout Service in the same namespace as the `EnvoyExtensionPolicy` avoids unnecessary cross-namespace `ReferenceGrant` complexity during the initial implementation.

---

# 8. EnvoyProxy configuration

Existing EnvoyProxy:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: EnvoyProxy
metadata:
  name: envoy-ai-gateway-basic
  namespace: envoy-ai-gateway-system
```

Add:

```yaml
spec:
  filterOrder:
    - name: envoy.filters.http.ext_proc
      before: envoy.filters.http.buffer
```

Preserve existing configuration such as:

```yaml
spec:
  logging:
    level:
      default: warn

  telemetry:
    metrics:
      prometheus: {}

  filterOrder:
    - name: envoy.filters.http.ext_proc
      before: envoy.filters.http.buffer
```

Do not remove existing logging or telemetry settings.

---

# 9. EnvoyExtensionPolicy

Create an `EnvoyExtensionPolicy` targeting the Agent Router Gateway or, preferably during testing, a dedicated test HTTPRoute.

Conceptually:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: EnvoyExtensionPolicy
metadata:
  name: nemo-guardrails
  namespace: envoy-ai-gateway-system
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: envoy-ai-gateway-basic

  extProc:
    - backendRefs:
        - name: <GUARDRAILS_CALL_OUT_SERVICE>
          port: <GUARDRAILS_CALL_OUT_GRPC_PORT>

      failOpen: false
      messageTimeout: 10s

      processingMode:
        request:
          body: Buffered
        response:
          body: Buffered
```

Replace:

```text
<GUARDRAILS_CALL_OUT_SERVICE>
<GUARDRAILS_CALL_OUT_GRPC_PORT>
```

with values discovered from the NVIDIA Helm deployment.

Do not invent the port.

---

# 10. Initial processing mode

Use:

```yaml
processingMode:
  request:
    body: Buffered
  response:
    body: Buffered
```

Initially.

Reason:

Agent Router's own body-processing configuration currently uses:

```text
request_body_mode: BUFFERED
response_body_mode: BUFFERED
```

when body processing is enabled.

NVIDIA supports streaming checks, but streaming should be introduced only after the basic integration is working.

Do not start with:

```text
FullDuplexStreamed
```

unless there is a concrete requirement.

---

# 11. Fail-open policy

Use:

```yaml
failOpen: false
```

for the initial security posture.

If NeMo is unavailable, the request should fail instead of silently bypassing the guardrail.

This can later become configurable depending on the application's risk profile.

---

# 12. Verify generated filter chain

After applying the EnvoyProxy and EnvoyExtensionPolicy, inspect the live Envoy configuration:

```bash
kubectl -n envoy-gateway-system exec \
  envoy-envoy-ai-gateway-system-envoy-ai-gateway-basic-2e891l2g7s \
  -c envoy -- \
  curl -s http://127.0.0.1:19000/config_dump |
  jq '.. | objects | select(.http_filters?) | .http_filters | map(.name)'
```

For an AI Gateway route, the desired chain should look approximately like:

```text
[
  "envoy.filters.http.ext_proc",
  "envoy.filters.http.buffer",
  "envoy.filters.http.ext_proc/aigateway",
  ...,
  "envoy.filters.http.router"
]
```

There may be additional filters depending on the Gateway configuration.

The critical ordering requirement is:

```text
envoy.filters.http.ext_proc
        BEFORE
envoy.filters.http.buffer
        BEFORE
envoy.filters.http.ext_proc/aigateway
```

Do not proceed to production validation if the ordering is different.

---

# 13. Verify the actual extproc configuration

Inspect the generated extproc filters:

```bash
kubectl -n envoy-gateway-system exec \
  envoy-envoy-ai-gateway-system-envoy-ai-gateway-basic-2e891l2g7s \
  -c envoy -- \
  curl -s http://127.0.0.1:19000/config_dump |
  jq '.. | objects |
      select(.name? == "envoy.filters.http.ext_proc" or
             .name? == "envoy.filters.http.ext_proc/aigateway")'
```

Verify that there are now two distinct processors:

```text
envoy.filters.http.ext_proc
envoy.filters.http.ext_proc/aigateway
```

The generic `ext_proc` should point at the NVIDIA guardrails-callout gRPC Service.

The `/aigateway` processor should continue pointing at:

```text
ai-gateway-extproc-uds
```

Do not modify the Agent Router extproc configuration.

---

# 14. Test with a dedicated route first

Before attaching the guardrail to the entire Gateway, create/use a dedicated test HTTPRoute.

Test cases should include:

1. Normal allowed request.
2. Prompt that should trigger an input guardrail.
3. Response that should trigger an output guardrail.
4. NeMo unavailable.
5. Large request body.
6. Large response body.
7. Streaming response, if streaming is supported by the application.
8. Provider/model translation performed by Agent Router.

Confirm that NeMo sees the expected OpenAI-compatible/client-facing representation.

---

# 15. Important Agent Router behavior

Do not modify:

```text
envoy.filters.http.ext_proc/aigateway
```

directly.

Do not patch the generated Envoy config.

Do not create an `EnvoyExtensionPolicy` that tries to reference:

```text
envoy.filters.http.ext_proc/aigateway
```

because the Envoy Gateway CRD does not expose that filter name in `filterOrder`.

Do not fork Agent Router.

Do not introduce a custom guardrail proxy.

The supported composition mechanism is:

```text
EnvoyExtensionPolicy
        +
EnvoyProxy.spec.filterOrder
        +
Agent Router extension-server injection
```

---

# 16. Why this architecture was selected

Agent Router PR #963 was specifically intended to improve external processor composability.

The previous design used an `EnvoyExtensionPolicy` for the AI Gateway processor, which made it difficult for users to add their own external processors before/after the AI Gateway processor.

PR #963 moved the AI Gateway extproc insertion into Agent Router's extension-server/xDS logic.

The v1.1.0 implementation recognizes the Envoy buffer filter and positions the AI Gateway extproc appropriately relative to it.

Therefore, the user extproc can be positioned before the buffer using:

```yaml
filterOrder:
  - name: envoy.filters.http.ext_proc
    before: envoy.filters.http.buffer
```

and Agent Router can then insert its own:

```text
envoy.filters.http.ext_proc/aigateway
```

after the buffer.

This gives:

```text
User ext_proc
    ↓
Buffer
    ↓
Agent Router ext_proc
```

without requiring Envoy Gateway to know about the private `/aigateway` filter name.

---

# 17. Desired final architecture

```text
                         Kubernetes
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Envoy Gateway        │
                  │ Agent Router v1.1.0  │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Envoy ext_proc       │
                  │ generic filter       │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ NVIDIA               │
                  │ guardrails-callout   │
                  │                      │
                  │ NeMo Guardrails      │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Envoy buffer         │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Agent Router         │
                  │ ext_proc/aigateway   │
                  └──────────┬───────────┘
                             │
                             ▼
                         LLM backend
```

No custom proxy is required.

---

# 18. Implementation checklist

## Discovery

* [ ] Determine NVIDIA NeMo Guardrails chart/version.
* [ ] Render the NVIDIA chart.
* [ ] Identify the guardrails-callout Deployment.
* [ ] Identify the guardrails-callout Service.
* [ ] Identify the actual gRPC extproc port.
* [ ] Confirm the callout Service is reachable from the Envoy data plane.

## Envoy configuration

* [ ] Add `envoy.filters.http.ext_proc` before `envoy.filters.http.buffer` to `EnvoyProxy.spec.filterOrder`.
* [ ] Deploy `EnvoyExtensionPolicy`.
* [ ] Point `backendRefs` at the NVIDIA guardrails-callout Service.
* [ ] Set `failOpen: false`.
* [ ] Start with `Buffered` request/response bodies.
* [ ] Do not alter Agent Router's `/aigateway` extproc.

## Verification

* [ ] Confirm generic `envoy.filters.http.ext_proc` appears in the active chain.
* [ ] Confirm it appears before `envoy.filters.http.buffer`.
* [ ] Confirm `envoy.filters.http.ext_proc/aigateway` appears after the buffer.
* [ ] Confirm the generic extproc points at the NVIDIA callout.
* [ ] Confirm the AI Gateway extproc still points at `ai-gateway-extproc-uds`.

## Functional tests

* [ ] Allowed request succeeds.
* [ ] Input violation is blocked.
* [ ] Output violation is blocked/redacted according to NeMo policy.
* [ ] NeMo failure causes request failure with `failOpen: false`.
* [ ] Large bodies work.
* [ ] Agent Router provider translation still works.
* [ ] Streaming behavior is evaluated separately.

---

# 19. References

Agent Router PR #963:

```text
https://github.com/theagentrouter/agent-router/pull/963
```

Agent Router v1.1.0 source, extension-server filter injection:

```text
https://github.com/theagentrouter/agent-router/tree/v1.1.0
```

NVIDIA NeMo Guardrails microservices documentation:

```text
https://docs.nvidia.com/nemo/microservices/
```

NVIDIA guardrails-callout deployment documentation:

```text
https://docs.nvidia.com/nemo/microservices/25.11.0/set-up/deploy-as-microservices/guardrails/gcp-installation.html
```

Envoy Gateway `EnvoyExtensionPolicy` documentation:

```text
https://gateway.envoyproxy.io/docs/api/extension_types/
```

---

# Final recommendation

Implement this as:

```text
NVIDIA guardrails-callout
        ↓
EnvoyExtensionPolicy
        ↓
EnvoyProxy.filterOrder:
  ext_proc BEFORE buffer
        ↓
Agent Router v1.1.0 extension-server injection
        ↓
ext_proc/aigateway
```

Do **not** create another proxy and do **not** modify Agent Router.

The first implementation should use buffered request/response processing and `failOpen: false`.

The most important validation is the live Envoy filter chain:

```text
ext_proc
→ buffer
→ ext_proc/aigateway
→ ...
→ router
```

Once that ordering is confirmed, validate NeMo's request/response behavior before enabling it for all production routes.
