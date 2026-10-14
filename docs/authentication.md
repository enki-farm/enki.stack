# Authentication

## Status And Scope

Dex v2.44.0 is the single OIDC issuer for every browser-facing app. LibreChat
(chart 2.0.17) and Grafana (chart 13.2.2) trust Dex only; Dex trusts exactly one
upstream identity provider through a connector. Dex lives in `k8s/dex` as a core
component. The main deployment command installs it and applies Grafana's OIDC
overrides automatically; configure the files below before running deployment.
LibreChat remains an optional app and receives OIDC overrides through `--auth-dir`.

**Model authentication is not activated by Dex.** No model SecurityPolicy is
installed, and LibreChat still uses its existing model endpoint credential. Do
not expose the model API to untrusted networks until the machine-access
policies below are in place.

## Why Dex In Front Of The Provider

Even with a single provider, apps should not talk to it directly:

- Changing provider (or adding a second one) is a connector swap in Dex. The
  apps, their client IDs, redirect URIs and role mappings stay untouched.
- Apps without OIDC support can be placed behind an authenticating proxy that
  trusts the same Dex issuer, so every app shares one identity and group model.
- Group names are normalized once, in the connector, instead of per app.

The cost is one small, single-replica service (see Operations).

## Prerequisites

- kubectl, Helm, openssl and an existing Envoy Gateway. No Node.js is needed.
- A default StorageClass for Dex's 1Gi ReadWriteOnce PVC.
- HTTPS hostnames for Dex, chat and Grafana, reachable by browsers and app Pods.
- Gateway API HTTPRoute CRDs and the shared `envoy-ai-gateway-basic` Gateway.
- Existing monitoring and LibreChat dependencies, including Crawl4AI credentials.

The shared Gateway accepts HTTPS only on port 443 and requires the
`envoy-ai-gateway-system/zer0-garden-wildcard-tls` Secret before its listener can
serve traffic. Configure that certificate as described in
[the certificate guide](../k8s/certificates/README.md). The generated Dex
HTTPRoute and the checked-in chat, Grafana and `/laya` routes attach to the HTTPS
listener. For LAN use, provide split DNS and a certificate trusted by clients.
The auth installer does not configure TLS certificates.

## Configuration

Three small files describe a deployment; none is rendered by custom code.

| File | Purpose |
|---|---|
| `k8s/dex/auth.env` (ignored, from [auth.env.example](../k8s/dex/auth.env.example)) | Public URLs and the group values that grant access |
| `k8s/dex/connector.yaml` (ignored, copied from [connectors/](../k8s/dex/connectors)) | The one upstream provider |
| [config.base.yaml](../k8s/dex/config.base.yaml) and [templates/](../k8s/dex/templates) | Provider-independent Dex, LibreChat, Grafana and route templates |

`auth.env` holds no secrets. Group values are written exactly as the connector
emits them, because the apps match on the `groups` claim:

- `CHAT_GROUP` is the group required to use LibreChat.
- `GRAFANA_VIEWER_GROUP` and `GRAFANA_ADMIN_GROUP` map to Grafana Viewer and
  organization Admin; an empty admin group grants nobody Admin. A user in neither
  group is denied (`role_attribute_strict`).

Secrets are never written to files. Connector secrets are written as `$NAME` in
`connector.yaml`; the installer reads each `$NAME` from your terminal
environment, stores it in the `dex/dex-secrets` Secret and Dex expands it at
start-up. Client secrets for LibreChat and Grafana are generated with `openssl`.

## Connectors

Only the connector file differs between providers. Copy the one you need:

```bash
cp k8s/dex/connectors/github.example.yaml k8s/dex/connector.yaml
```

Any other [Dex connector](https://dexidp.io/docs/connectors/) (Google, generic
OIDC, LDAP, ...) works the same way: add a `connectors:` list, reference secrets
as `$NAME`, and use `@DEX_ISSUER@` for the redirect URI base. The group values in
`auth.env` then follow that connector's group format.

### GitHub

[connectors/github.example.yaml](../k8s/dex/connectors/github.example.yaml)

1. Register an OAuth App at github.com/settings/developers with callback
   `<issuer>/callback`.
2. Export `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in your terminal.
3. List the org and teams in the connector. With `teamNameField: slug`, groups
   reach the apps as `<org>:<team-slug>` (for example `acme:ai-users`), and that
   is what `auth.env` must contain.

A user must belong to at least one listed org or team to log in. Organization
owners must approve the OAuth App, otherwise members cannot be resolved and are
refused. Email comes from the public or primary GitHub address.

### Microsoft Entra ID

[connectors/entra.example.yaml](../k8s/dex/connectors/entra.example.yaml)

1. Register an app in your tenant with redirect URI `<issuer>/callback`.
2. Add the delegated `Directory.Read.All` permission and grant admin consent
   (`https://login.microsoftonline.com/<tenant>/adminconsent?client_id=<id>`).
   Dex needs it to list groups.
3. Export `ENTRA_CLIENT_ID` and `ENTRA_CLIENT_SECRET` in your terminal.
4. Set `tenant` to the tenant id or name. `common` and `organizations` do not
   return groups.

Groups reach the apps as display names. Set `groupNameFormat: id` to receive
object ids instead, which survive renames. With `useGroupsAsWhitelist` only the
listed groups appear in the token, and members of none cannot log in. Dex
queries Microsoft Graph for groups, so Graph group-overage is not an issue.

### Switching Provider

Replace `connector.yaml`, update the group values in `auth.env`, export the new
secrets and rerun the installer. Apps are reconfigured with the new group values
by reapplying the generated overrides. Dex subjects are scoped to the connector,
so users appear as new identities: existing LibreChat chat histories and Grafana
users belong to the old login and are not merged.

Stale groups: connectors resolve groups at login and on refresh. Removing a user
from a group does not immediately end existing sessions; refresh tokens are
capped at 7 days in the Dex config.
## Installation

Render and inspect secret-free configuration without cluster access:

```bash
cp k8s/dex/auth.env.example k8s/dex/auth.env
cp k8s/dex/connectors/github.example.yaml k8s/dex/connector.yaml
bash scripts/install-auth.sh --dry-run
```

After configuring HTTPS/DNS and exporting the connector secrets in your terminal:

```bash
bash scripts/install-auth.sh
bash k8s/addons/librechat/install-librechat.sh --auth-dir .generated/auth
bash scripts/install-monitoring.sh --platform dgx-spark --auth-dir .generated/auth
```

Use `--platform aks` for AKS. The generated configuration targets the `dex`,
`librechat` and `observability` namespaces and existing release names. App
installers reject a conflicting namespace. The default deployment script does
not install or enable auth automatically.

Secrets are submitted to Kubernetes through stdin, not command-line arguments.
Dex settings live in the `dex/dex-config` ConfigMap (no secrets) and the
`dex/dex-secrets` Secret; each app receives only its own `oidc-credentials`
Secret. Stored client secrets, connector secrets and LibreChat's OIDC session
secret are reused on reruns, so you only need to export a connector secret when
it changes. Values are generated only when the Secret is verifiably absent; a
cluster read failure aborts instead of regenerating credentials.

Generated overrides contain no secrets, only URLs and group names. They are
written under the ignored `.generated/auth` directory with restrictive
permissions. Treat cluster Secrets as sensitive: Kubernetes Secret storage is
not encryption by itself. Use suitable RBAC and encryption at rest.

Reapply the auth overrides on every subsequent app upgrade. Omitting `--auth-dir`
after OIDC provisioning fails instead of restoring legacy login. Dry-runs do not
inspect cluster state, and manual Helm commands can bypass this installer guard.
After installing overlays, reapply the generated app routes if their hostnames
or listener differ from the static routes. The model hostname is unchanged.

### Migrating From The Earlier Local-Accounts Setup

Earlier versions rendered Dex settings from a JSON profile with local password
accounts and Node.js tooling. Both are gone. Move to a connector as described
above; `k8s/dex/profile.json` and the old `dex/dex-config` Secret are no
longer used (remove the Secret with `kubectl -n dex delete secret dex-config`
once the new ConfigMap is serving). The Dex PVC can be kept, but users from the
old local connector do not carry over.

## Operations And Verification

Dex is single-replica with SQLite and Recreate updates. Back up the PVC and
the `dex-secrets` Secret together using a consistent SQLite backup or stopped
workload snapshot. The PVC holds signing keys and refresh state. This is not an
HA deployment. Do not delete the PVC as a restart procedure.

Rotating a connector secret: export the new value and rerun the installer; it
updates `dex-secrets` and restarts Dex. Client-secret rotation needs a
coordinated Dex and app rollout; no automatic rotation workflow is supplied.
Only rerun installers while the connector's secrets and provider are available.
Add login rate limiting at the public entry point before production exposure;
no rate-limit service is installed for the login endpoints.

Grafana grants organization Admin, not global server Admin. Normal basic login is
disabled. Emergency recovery requires Kubernetes administrative access and an
explicit temporary configuration change; no public bypass route is installed.
Signing out of one app does not guarantee global logout from Dex or another app.

Verify app login, denied users, private chat histories and Viewer/Admin roles
with staging hostnames before changing production login. The shell tests cover
rendering for both example connectors and installer guards, not live
LibreChat/Grafana browser sessions or provider behavior. Backend network
isolation, trusted proxy headers, DNS/TLS and CNI enforcement still need
cluster-specific validation.

```bash
bash scripts/tests/test-auth-installers.sh
bash scripts/tests/test-deploy.sh
kubectl kustomize k8s/dex
```

## Model Compatibility Gate

The pinned Dex source and a runtime test against a real v2.44.0 container
(since removed with the Node tooling) established that:

- Access tokens and ID tokens share signing keys, issuer, audience and claim shape.
- Both contain `at_hash`; its mere presence is not a token-type discriminator.
- There is no `token_use`, dedicated access-token `typ`, or application scope claim.
- v2.44.0 does not provide the client-credentials grant described in newer docs.

Consequently, issuer/signature/audience checks alone do not prove a bearer JWT is
an OAuth access token rather than an ID token. No model policy that accepts Dex
tokens is generated.

Next evaluate a supported token-type-isolated issuer release or authorization
component. Alternatively, explicitly approve Dex's documented audience-bound
identity-JWT pattern with a dedicated model audience and constrained trusted
clients. That changes the strict access-token contract and requires separate
validation, not acceptance of arbitrary app tokens.

Before model activation, prove live LibreChat credential forwarding for chat,
agents, titles, model listing and refresh, protect all generated AI Gateway
routes and `/laya`, and block direct predictor bypass. Preserve future human
OAuth clients, machine principals and separately revocable API keys as distinct
credential paths. None is implicitly enabled by this foundation.

Source anchors: [Dex token implementation](https://github.com/dexidp/dex/blob/v2.44.0/server/oauth2.go),
[Dex supported grants](https://github.com/dexidp/dex/blob/v2.44.0/server/server.go),
[LibreChat role checks](https://github.com/danny-avila/LibreChat/blob/v0.8.8/api/strategies/openidStrategy.js).

## Machine Access (Gateway API Keys)

Dex covers people. Machine and developer-tool callers (in-cluster agents, CI,
IDE tools, OpenAI SDK scripts) use static API keys enforced by Envoy Gateway.
This is a design, not an activated feature: no policy is installed yet. It
keeps the Dex model-access gate above intact.

Keycloak (or any IdP with `client_credentials`) was evaluated and rejected. It
would replace Dex, add Keycloak and a database, and give short-lived tokens that
OpenAI-style clients, which expect a static `api_key`, do not refresh. It also
does not improve per-client budgets. Revisit only if federated, short-lived
workload identity becomes a requirement.

### Mechanism (native, no ext_authz)

Everything below is built into the pinned Envoy Gateway v1.8.1 and AI Gateway
v1.1.0. No external authorization service is required.

| Need | Mechanism |
|---|---|
| Authenticate | `SecurityPolicy.apiKeyAuth` with an Opaque Secret of `<clientID>: <key>` entries, `extractFrom` the `Authorization` header |
| Client identity | `forwardClientIDHeader: x-client-id`; `sanitize: true` removes the key before the backend |
| Per-model access | One `SecurityPolicy` per AIGatewayRoute (one route per model) with an `authorization` rule using a `headers` principal on `x-client-id` and `defaultAction: Deny` |
| Token budgets | `llmRequestCosts` on the AIGatewayRoute plus a `BackendTrafficPolicy` global rate limit, one rule per client (`x-client-id` Exact) |
| Attribution | `x-client-id` in access logs and traces |

Store keys without a `Bearer ` prefix. Clients still send
`Authorization: Bearer <key>`, so OpenAI SDKs work unchanged.

Use one client ID per caller. Rotation is a Secret edit; revocation is deleting
the entry. Keys have no expiry or scopes.

### Verified On The Cluster

Checked on Envoy Gateway v1.8.1 with an isolated throwaway route
(`m2m-test.invalid`) and Secret, since removed. Live routes were not touched.

- A `SecurityPolicy` targeting the HTTPRoute that AI Gateway generates from an
  AIGatewayRoute (same name and namespace) is accepted and enforced. Policies
  can therefore be attached per model route.
- No key or an unknown key returns 401. A valid key returns 200.
  Both `Authorization: Bearer <key>` and a raw `Authorization: <key>` are accepted.
  A key sent in `x-api-key` is rejected when `extractFrom` lists only `Authorization`.
- A `headers` principal on `x-client-id` allows the matching client (200) and
  denies others (403) under `defaultAction: Deny`.
- A client cannot forge identity: presenting its own key with a forged
  `x-client-id` of another client was denied (403); the gateway overwrites the header.
- **`authorization.rules[].cel` did not enforce on v1.8.1.** CEL-only rules
  allowed requests whose expression was false, both on `x-ai-eg-model` and on
  `x-client-id`. Do not use CEL for model or client decisions on this version.
  Because of this, `x-ai-eg-model` visibility to the authorization filter is
  unproven, and per-model access relies on per-route policies instead.

New keys and policy edits take several seconds to reach the proxy. Expect brief
401/403 right after applying.

### Not Yet Verified

- Token rate limiting: needs Envoy Gateway's rate-limit service with Redis,
  which is not enabled in `helm/gateway/envoy-gateway-values.yaml`. The AI
  Gateway quota default currently fails open.
- `llmRequestCosts` accounting for streaming responses (charged after the
  stream completes, so a limit can be overshot once).
- Compatibility of LibreChat with a real key in `ENKI_GATEWAY_API_KEY` (chat,
  agents, titles, model listing).

### Limits Of v1.8.1

- Per-client budgets are one rate-limit rule per client in a single
  `BackendTrafficPolicy`. Adding a client edits both the key Secret and that
  policy.
- Rate-limit `limit.fromMetadata` (dynamic budgets from request metadata) is
  merged upstream but targets Envoy Gateway v1.9; the pinned release silently
  ignores the field.
- An ext_authz or ext_proc service becomes worthwhile only for dynamic
  per-client budgets after upgrading to Envoy Gateway 1.9 or later, key expiry
  or self-service issuance, or keys stored outside Kubernetes.

### Activation Checklist

1. Configure the Gateway TLS Secret; keys must only travel over TLS.
2. Enable the Envoy Gateway rate-limit service with Redis.
3. Create the key Secret (generated with `openssl`, kept in the ignored `.env`).
4. Add one `SecurityPolicy` per AIGatewayRoute and one for the `/laya` route.
5. Add `llmRequestCosts` and the per-client `BackendTrafficPolicy`.
6. Replace the `ENKI_GATEWAY_API_KEY` placeholder and re-verify LibreChat.
7. Block direct access to predictor Services so the gateway cannot be bypassed.