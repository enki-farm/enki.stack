# LibreChat Admin Panel

Optional standalone upstream preview panel for LibreChat v0.8.8. No custom
image, database, PVC, OIDC client or database credentials are added. The official
GHCR image is pinned to a multi-platform digest with linux/amd64 and linux/arm64
manifests. Upgrade this digest only after checking the panel against the pinned
LibreChat APIs; the documented minimum version is not a compatibility guarantee.

## Install

Add these nonsecret settings to the ignored Dex auth environment file:

```dotenv
CHAT_ADMIN_URL=https://chatadmin.zer0.garden
CHAT_ADMIN_GROUP=enki-farm:core
```

Keep `CHAT_GROUP=enki-farm:core` for the current deployment. The chat gate and
admin group are independent; an administrator must pass both. The connector
must emit these groups in the ID token. A new core member must first sign into
ordinary chat to create their account, then use Settings > General > Admin Panel.

Use a scoped kubeconfig pointing to Spark for installer commands (the scripts
inherit `KUBECONFIG`; do not change the global current context). For example:

```bash
umask 077
spark_config="$(mktemp)"
kubectl --context spark config view --minify --flatten --raw > "$spark_config"
export KUBECONFIG="$spark_config"
bash scripts/install-auth.sh --dry-run
bash k8s/addons/librechat/install-librechat.sh \
  --auth-dir .generated/auth --with-admin-panel --dry-run
```

Inspect the rendered configuration. Back up MongoDB, uploads and matching
LibreChat credentials before upgrading; follow the existing
[upload migration procedure](../librechat/README.md#existing-upload-migration)
if the installer requires an archive. Upgrades use Recreate and interrupt chat.
During an agreed maintenance window:

```bash
bash scripts/install-auth.sh
bash k8s/addons/librechat/install-librechat.sh \
  --auth-dir .generated/auth --with-admin-panel
```

Supply `--uploads-archive /secure/backup/librechat-uploads.tar` only if migrating
unmounted uploads. Keep `--with-admin-panel` and `--auth-dir` on repeat installs.
Delete the temporary scoped kubeconfig after use and unset `KUBECONFIG` (or
restore its previous value); it contains credentials and must not enter Git.

The panel uses `http://librechat-librechat.librechat.svc.cluster.local:3080`
server-side and the configured public chat URL for browser OAuth redirects.
Dex registers `/api/admin/oauth/openid/callback` on the chat hostname using the
existing LibreChat client. `ADMIN_PANEL_URL` provides the return URL and chat link.
The route attaches only to the HTTPS listener. Verify DNS and IPv6 routing,
Cloudflare behavior, served certificate and any tunnel/origin hostname routing;
DNS resolution alone does not prove the new hostname reaches Spark. The existing
`*.zer0.garden` certificate covers this hostname if Ready and correctly served.

## Security And Configuration Policy

The panel is SSO-only with secure cookies and a 30-minute idle timeout. LibreChat
also disables email login and its override; v0.8.8's actual local-admin endpoint
is `POST /api/admin/login/local`, protected by the email-login validator. Test
that endpoint directly after deployment, not just whether the form is hidden.
Admin APIs remain reachable on the public chat hostname and must enforce access
independently of the panel. Kubernetes ServiceAccount credentials are not mounted.

`OPENID_ADMIN_ROLE` maps exact ID-token group membership to full `ADMIN` on login.
Core membership is authoritative for OIDC ADMIN roles, including accounts
manually promoted in the panel. Removing chat membership may reject login before
stored ADMIN is demoted. Existing sessions and cached claims are not immediately
revoked by a team change. The idle timeout is not an absolute lifetime or a
revocation SLA. Keep the current `OPENID_REUSE_TOKENS=false` policy: admin refresh
is disabled by LibreChat, and token expiry may require another SSO login even
while the panel is active. Enabling refresh is a separate security decision.

The panel can assign `access:admin` and other system grants independently of the
ADMIN role. To retain the core-only policy, do not delegate these capabilities
to users/groups/roles outside core, and audit existing grants before widening
`CHAT_GROUP`. A separate admin-group setting is not a universal deny rule over
LibreChat's capability model.

Prefer Git for configuration. Full ADMIN can still create persistent MongoDB
overrides that supersede YAML and survive Helm upgrades. Reconcile emergency UI
edits into Git and clear redundant overrides. This preference is operational,
not an enforced read-only restriction. Back up MongoDB and deployment credentials
together. Existing panel session keys are reused; read errors or invalid stored
keys abort installation instead of silently rotating them. Intentional session
key rotation invalidates panel cookies, not all LibreChat API tokens.

## Verify And Recover

```bash
bash scripts/tests/test-auth-installers.sh
bash scripts/tests/test-librechat-admin.sh
bash scripts/tests/test-librechat-rag.sh
kubectl -n librechat rollout status deployment/librechat-admin-panel
kubectl -n librechat get httproute librechat-admin-panel -o yaml
curl --fail https://chatadmin.zer0.garden/health
```

Check route `Accepted` and `ResolvedRefs`, core-user SSO, the settings link,
secure session cookies, and rejected unauthorized admin API requests. Test an
authenticated non-admin and membership removal using isolated test identities;
do not widen production chat access or remove real core members for testing.
Confirm ordinary chat, RAG, uploads and web search still work. Browser SSO and
revocation checks require real identities and are not covered by rendering tests.

Emergency containment: remove the panel route and scale its Deployment to zero.
This does not disable admin APIs on the chat host. Use the pinned LibreChat's
supported account-ban/session-revocation mechanisms and verify API rejection;
if necessary restrict admin API traffic at the gateway as an explicit incident
action. Do not describe panel-key rotation as global token revocation.

For a panel-only rollback, keep both auth settings and the admin Helm override
so group policy remains authoritative. After a failed rollout, retain the session
Secret, MongoDB and all PVCs. A routine installer rerun without admin settings
is refused when the panel ConfigMap/Deployment exists. Removing the admin role
mapping is a separate authorization migration: revoke unwanted roles/grants and
sessions first, then deliberately remove panel resources, callbacks and values.
Never delete the shared LibreChat namespace as addon cleanup. Manual Helm/kubectl
commands can bypass installer guards and require the same review.