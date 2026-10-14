# Authenticated Token-Limited Alias

`default-rate-limited` reuses the running `default` model, with no additional
GPU allocation. Its backend reference rewrites the upstream model to `default`.
Only the shared Gateway's `https` listener serves this alias.

Requests must also send `x-ai-eg-model: default-rate-limited`. Native API-key
authentication runs before AgentRouter extracts the model from the JSON body;
with an unprotected default route sharing this hostname, the header is needed
to select the protected route before authentication. Body-only alias requests
are denied (403), including requests with valid keys. OpenAI SDK callers can
set `extra_headers={"x-ai-eg-model": "default-rate-limited"}`. A dedicated
hostname is the alternative when header-free SDK compatibility is needed.

Each authenticated client receives **1,000 total input + output tokens per
minute**. Envoy authenticates the API key, overwrites `x-client-id` with the
client ID, and removes the credential before forwarding upstream. A single
`Distinct` rule gives each client its own bucket. Both policies target the
HTTPRoute generated from this alias, not the shared backend or Gateway.

## Install

Deploy `models/default` and the shared Gateway first. Then:

```bash
bash scripts/install-ai-gateway.sh --context spark --enable-usage-rate-limiting
bash scripts/install-rate-limited-model-example.sh --context spark
```

For a cluster previously managed by another CRD field manager, first review a
server-side dry-run/diff. `--take-crd-ownership` explicitly transfers ownership
of the versioned Gateway API and Envoy extension CRD definitions. It is not
needed on every run and must not be used to overwrite provider-managed schemas.
The vendored Gateway API v1.6.1 bundle preserves the experimental channel used
by Envoy Gateway; Helm owns the safe-upgrade admission policy.

The example installer checks a ready model, Gateway, Redis and fail-closed
limiter, then provisions `demo-client-a` and `demo-client-b` in the
`inference/default-rate-limited-api-keys` Secret. Raw keys contain no `Bearer`
prefix. Their local copy is `.env.rate-limit-example` (gitignored, mode 600).
Reruns reuse keys; a missing local file is recovered from the Secret, while
mismatched local/cluster keys fail rather than silently rotating credentials.
No Secret or real key is committed. `--dry-run` renders manifests without
generating keys or changing the cluster. Treat `--env-file` as a private path:
custom paths are not automatically gitignored.

Use `--gateway-url https://...` and, for a private CA, `--ca-cert PATH`.
Certificate verification is never disabled. Do not send keys over HTTP.

## Request

Run from the repository root. This sends the credential through curl's stdin
configuration, keeping it out of process arguments:

```bash
source .env.rate-limit-example
printf 'header = "Authorization: Bearer %s"\n' "$DEMO_CLIENT_A_API_KEY" |
  curl --config - --silent --show-error --fail-with-body \
    -H 'Content-Type: application/json' \
    -H 'x-ai-eg-model: default-rate-limited' \
    https://ai.zer0.garden/v1/chat/completions \
    -d '{"model":"default-rate-limited","messages":[{"role":"user","content":"Hello"}],"max_tokens":32}'
unset DEMO_CLIENT_A_API_KEY DEMO_CLIENT_B_API_KEY
```

Missing or invalid credentials with the model header return 401. The authorization allow-list contains
the two demo IDs; adding a client requires updating the Secret and allow-list,
but not the equal-budget rate-limit rule. Rotate by coordinating an explicit
Secret update and local file update; revoke by removing the client's Secret
entry. Proxy updates take several seconds. Static keys have no expiry.

## Verify And Recover

Check policy acceptance and workload readiness:

```bash
kubectl --context spark -n inference get aigatewayroute,httproute,securitypolicy,backendtrafficpolicy
kubectl --context spark -n ai-gateway-ratelimit get pods,pvc
kubectl --context spark -n envoy-gateway-system get deployments
```

Repeat the live checks with fresh minute budgets:

```bash
bash scripts/tests/test-rate-limited-model-live.sh
bash scripts/tests/test-rate-limited-model-resilience.sh spark
```

The first test deliberately exhausts both demo clients. Run the second after
the next minute window starts. It replaces the Redis pod and temporarily
restricts limiter/Redis network ingress; its EXIT trap restores connectivity.
These tests issue real model requests and require an enforcing NetworkPolicy
implementation. Never run the outage test against production without a window.

Test both clients, sum response `usage.total_tokens` within the same minute,
exhaust client A, and confirm 429 for A but success for B. Spoofing or duplicating
`x-client-id` must not create a fresh bucket. Repeat with streaming responses
and their final usage data, then verify recovery after the next minute boundary.
Stopping the rate-limit service should return 500 for this alias, not 200 or
quota 429; restore it immediately after testing. Restarting Redis without
removing its PVC should retain counters within the current minute.

Tokens are charged **after response completion**. An admitted response or
concurrent streams can exceed the budget; streams are not interrupted. Later
requests are rejected until the minute window resets. This is usage-based
admission control, not hard token reservation.

Existing `default` and other routes remain unchanged and unprotected. They and
direct predictor access bypass this example quota. The model catalog remains
under its existing access policy, and this alias is visible there. This example
does not protect the entire platform or enable Dex model authentication.

On installation failure, the helper removes a newly created alias if its
protection could not be verified. To withdraw it manually, delete the
`default-rate-limited-model` AIGatewayRoute first, then its two policies and
Secret. Do not remove authentication while leaving the alias public. Keep the
Redis PVC. Helm rollback does not roll back CRDs; inspect compatibility before
restoring captured release revisions, and never delete CRDs as a recovery step.