# Authentication

For fresh CLI deployments, use the [profile-driven CLI](cli.md): it collects
active application groups, accepts GitHub/Entra or arbitrary Dex connectors,
and stores supplied/generated credentials in ignored local `secrets.yaml`.
The CLI manages Dex login for Grafana, Hub and the LibreChat addon.

## LibreChat

LibreChat uses Dex client `librechat` and callback
`https://chat.<domain>/oauth/openid/callback`. The `auth` component registers
this client and owns its stable secret. LibreChat requires the installed `auth`
component and copies the client credential from `dex/dex-secrets` into its
managed application Secret; it does not rotate the Dex credential.

Login automatically redirects to Dex using PKCE, state and nonce. Email/password
login, local registration and public shared links are disabled. Social
registration is enabled only for the OpenID provider, and access requires the
`groups` ID-token claim to contain `auth.chat_group`, falling back to
`auth.hub_group` when `chat_group` is empty. A missing group fails installation
rather than allowing unrestricted access.
GitHub group claims include the organization prefix (for example,
`enki-farm:core`, not `core`); use the exact claim emitted by the connector.

To upgrade an existing CLI deployment that predates the LibreChat client:

```bash
enkictl component install auth -p profiles/customer/profile.yaml
enkictl addon install librechat -p profiles/customer/profile.yaml
```

The auth upgrade adds the new credential without changing Grafana/Hub keys and
rolls Dex when its config changes. Missing credentials for an already registered
client must be restored, not silently regenerated. Self-signed deployments
mount the platform trust bundle into LibreChat via `NODE_EXTRA_CA_CERTS`;
TLS verification is never disabled. Complete the provider login in a browser
to verify both member and nonmember access after deployment.

## Kubeflow Hub

Core Hub uses the existing Dex issuer with client `hub`, callback
`https://hub.zer0.garden/oauth2/callback`, and group `enki-farm:core`.
Configure the base `domain` and `auth.hub_group` in the profile. Its native
Envoy OIDC/JWT policy is scoped only to Hub's HTTPS route; no OAuth2 Proxy is
installed. Publication requires a maintenance window for the Dex update and
browser/nonmember/spoofing checks. See [Hub setup](kubeflow-hub.md).

## Status And Scope

Dex v2.44.0 is the OIDC issuer for Grafana, Hub and LibreChat. Dex trusts exactly one
upstream identity provider through a connector. Dex lives in `k8s/dex`; the
`auth` component provisions it independently, while `monitoring` and
`kubeflow-hub` provision their own credential copies and routes.

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

- Python 3.12+, kubectl, Helm and a cluster with installation permissions.
- The platform StorageClass for Dex's 1Gi ReadWriteOnce PVC.
- DNS/network access to HTTPS hostnames for Dex and the selected core apps.
- One configured identity-provider connector.

The shared Gateway accepts HTTPS only on port 443 and requires the
`envoy-ai-gateway-system/enki-platform-tls` Secret before its listener can
serve traffic. Configure that certificate as described in
[the certificate guide](../k8s/certificates/README.md). The generated Dex
HTTPRoutes attach to the HTTPS listener. For LAN use, provide split DNS and a
certificate trusted by clients. Installing `auth` includes missing `ai-gateway`
and `certificates` dependencies automatically.

## Configuration

Three files beside the selected profile describe deployment intent.

| File | Purpose |
|---|---|
| `profile.yaml` | Domain, TLS settings and selected app authorization groups |
| `connector.yaml` | The one upstream provider; credentials referenced as `$NAME` |
| `secrets.yaml` (ignored, mode 0600) | Supplied connector/solver and generated application credentials |

The profile holds no secrets. Group values are written exactly as the connector
emits them, because the apps match on the `groups` claim:

- `auth.hub_group` gates Hub access.
- `auth.chat_group` gates LibreChat access, defaulting to `auth.hub_group`.
- `auth.grafana_viewer_group` and `auth.grafana_admin_group` map to Grafana Viewer and
  organization Admin; an empty admin group grants nobody Admin. A user in neither
  group is denied (`role_attribute_strict`).

Secrets are stored in the local private `secrets.yaml`; back it up securely with
the CLI inventory. Put `$NAME` references in `connector.yaml` and the matching
values under `connector` in `secrets.yaml`. Dex receives them through
`dex/dex-secrets` and expands references at startup. Stable Grafana/Hub/LibreChat client
secrets are generated by the auth component; consumer namespaces, database
secrets and registry credentials are created only when their components install.

## Connectors

Only the connector file differs between providers. Copy the one you need:

```bash
cp k8s/dex/connectors/github.example.yaml profiles/customer/connector.yaml
```

Any other [Dex connector](https://dexidp.io/docs/connectors/) (Google, generic
OIDC, LDAP, ...) works the same way: add a `connectors:` list, reference secrets
as `$NAME`, and use `@DEX_ISSUER@` for the redirect URI base. The group values in
the profile then follow that connector's group format.

### GitHub

[connectors/github.example.yaml](../k8s/dex/connectors/github.example.yaml)

1. Register an OAuth App at github.com/settings/developers with callback
   `<issuer>/callback`.
2. Set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` in the profile's secrets file.
3. List the org and teams in the connector. With `teamNameField: slug`, groups
   reach the apps as `<org>:<team-slug>` (for example `acme:ai-users`), and that
   is what the profile's app-group settings must contain.

A user must belong to at least one listed org or team to log in. Organization
owners must approve the OAuth App, otherwise members cannot be resolved and are
refused. Email comes from the public or primary GitHub address.

### Microsoft Entra ID

[connectors/entra.example.yaml](../k8s/dex/connectors/entra.example.yaml)

1. Register an app in your tenant with redirect URI `<issuer>/callback`.
2. Add the delegated `Directory.Read.All` permission and grant admin consent
   (`https://login.microsoftonline.com/<tenant>/adminconsent?client_id=<id>`).
   Dex needs it to list groups.
3. Set `ENTRA_CLIENT_ID` and `ENTRA_CLIENT_SECRET` in the profile's secrets file.
4. Set `tenant` to the tenant id or name. `common` and `organizations` do not
   return groups.

Groups reach the apps as display names. Set `groupNameFormat: id` to receive
object ids instead, which survive renames. With `useGroupsAsWhitelist` only the
listed groups appear in the token, and members of none cannot log in. Dex
queries Microsoft Graph for groups, so Graph group-overage is not an issue.

### Switching Provider

Replace the profile's `connector.yaml`, update app groups and supplied secrets,
then explicitly reconcile auth and consumers, for example
`enkictl component install auth monitoring kubeflow-hub -p profiles/customer/profile.yaml`.
Consumer-only installs do not silently reconcile already-installed auth.
Dex subjects are scoped to the connector, so users appear as new identities;
existing Grafana users are not merged.

Stale groups: connectors resolve groups at login and on refresh. Removing a user
from a group does not immediately end existing sessions; refresh tokens are
capped at 7 days in the Dex config.
## Installation

Configure only auth, or include consumer targets to collect their groups too:

```bash
enkictl init --component auth -p profiles/customer/profile.yaml
enkictl validate auth -p profiles/customer/profile.yaml --render
```

After configuring DNS and connector credentials:

```bash
enkictl component install auth -p profiles/customer/profile.yaml --dry-run
enkictl component install auth -p profiles/customer/profile.yaml
enkictl init --component monitoring --component kubeflow-hub -p profiles/customer/profile.yaml
enkictl component install monitoring kubeflow-hub -p profiles/customer/profile.yaml
```

Select `aks` or `dgx-spark` in the profile. Fixed component namespaces and release
names are retained. The no-target core install includes auth automatically.
Fresh installations only are supported; no old resources are silently adopted.

Secrets are submitted to Kubernetes through stdin, not command-line arguments.
Dex settings live in the `dex/dex-config` ConfigMap (no secrets) and the
`dex/dex-secrets` Secret; each app receives only its own `oidc-credentials`
Secret. Stored client credentials are preserved on reruns and consumer
removal/reinstallation; missing local generated values must be restored rather
than regenerated against retained data.

Helm overrides pass through stdin, not a generated auth directory. Treat local
and cluster Secrets as sensitive: Kubernetes Secret storage is not encryption
by itself. Use suitable RBAC and encryption at rest.

Use named component installs for upgrades rather than bypassing ownership with
manual Helm commands. Install dry-run performs read-only cluster preflight;
offline validation/rendering is available through `validate --render`.

Grafana's login form remains disabled, but Basic authentication is enabled for
the datasource sidecar's provisioning reload API. The sidecar uses credentials
from the `observability/grafana-admin` Secret; protect that Secret accordingly.

### Grafana Login Fails With A Missing Role Attribute

The message `IdP did not return a role attribute` can mean that the groups
returned by Dex do not match Grafana's configured role mapping, not that Dex
must supply a literal `role` claim. Compare the successful login's `groups` in
the Dex logs with `auth.grafana_viewer_group` and `auth.grafana_admin_group` in
the profile. For GitHub, use the full organization-prefixed claim, for example
`enki-farm:core`, rather than the bare team slug `core`.

Correct the profile and reconcile Grafana's configuration:

```bash
enkictl component install monitoring -p profiles/customer/profile.yaml
```

Retry browser login after Grafana rolls out. Keep `role_attribute_strict: true`;
disabling it would allow users outside the configured groups to receive a
default role instead of fixing the mismatch. Admin membership takes precedence
when the same group is configured for both roles.

### Migrating From The Earlier Local-Accounts Setup

Old JSON/local-account, environment-file and prior CLI installations are not
adopted. Back up PVCs, credentials and inventory, and operate old installations
with their original tooling. Do not delete state merely to bypass a conflict.

## Operations And Verification

Dex is single-replica with SQLite and Recreate updates. Back up the PVC and
the `dex-secrets` Secret together using a consistent SQLite backup or stopped
workload snapshot. The PVC holds signing keys and refresh state. This is not an
HA deployment. Do not delete the PVC as a restart procedure.

Rotating a connector secret: update the supplied local value and explicitly
reconcile `auth`; restart Dex to consume changed environment values.
Client-secret rotation needs a
coordinated Dex and app rollout; no automatic rotation workflow is supplied.
Only reconcile auth while the connector's secrets and provider are available.
Add login rate limiting at the public entry point before production exposure;
no rate-limit service is installed for the login endpoints.

Grafana grants organization Admin, not global server Admin. Normal basic login is
disabled. Emergency recovery requires Kubernetes administrative access and an
explicit temporary configuration change; no public bypass route is installed.
Signing out of one app does not guarantee global logout from Dex or another app.

Verify app login, denied users, private chat histories and Viewer/Admin roles
with staging hostnames before changing production login. CLI tests cover
rendering and lifecycle guards, not live browser sessions or provider behavior. Backend network
isolation, trusted proxy headers, DNS/TLS and CNI enforcement still need
cluster-specific validation.

```bash
python -m pytest cli/tests/test_components.py cli/tests/test_render.py
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

The optional [rate-limited alias example](../models/default-rate-limited/README.md)
now supplies a narrowly scoped implementation for AgentRouter v1.2.0 and Envoy
Gateway v1.9.2. Its two demo clients each receive 1,000 total tokens per minute,
using a trusted forwarded client ID and a `Distinct` rate-limit selector. It
does not activate authentication on existing models, Dex model access, or
LibreChat credential forwarding. The older verification notes below remain
historical evidence for v1.8.1.

On 2026-10-14, the example was validated on Spark with AgentRouter v1.2.0,
Envoy Gateway v1.9.2 and Envoy Proxy v1.39.3: valid demo keys succeeded,
missing/invalid keys with the explicit model header returned 401, exhausting
one client returned 429 without affecting the other, forged/duplicate client
headers did not change budgets, and final streaming usage was charged. The
original body-only `default` route still succeeded without authentication.
Native API-key authentication precedes body model extraction: the protected
alias requires `x-ai-eg-model: default-rate-limited`; body-only alias requests
are denied (403). This is not header-free SDK access or platform-wide security.

The same live checks verified minute-window recovery, Redis AOF persistence
after pod replacement, HTTP 500 during limiter and Redis network outages, and
successful recovery after connectivity was restored. The unprotected original
model continued serving during both outages. No production HA is implied.

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