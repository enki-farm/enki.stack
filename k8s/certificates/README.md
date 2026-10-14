# Gateway TLS Certificates

This directory issues a publicly trusted Let's Encrypt certificate for the
shared Envoy Gateway. Choose **one** DNS provider overlay: Cloudflare or Azure
DNS. Both issue the same SANs into the same Gateway-namespace Secret, so do not
apply both providers to one cluster.

The existing cert-manager installation (v1.21.1) is included in both platform
overlays. The Gateway serves HTTPS only, so configure its TLS Secret before
relying on Gateway traffic. Public `zer0.garden` deployments can use either DNS
provider flow below.

## Certificate And Gateway

The wildcard Certificate includes `zer0.garden` and `*.zer0.garden`; this covers
the apex and one-level names such as `auth`, `chat`, `grafana` and `ai`. Both
provider overlays target `envoy-ai-gateway-system/zer0-garden-wildcard-tls`.
The certificate is consumed by Envoy, not mounted into application Pods.

After creating the provider credential Secret, first apply its **staging**
overlay. Replace the placeholder ACME email and, for Azure, DNS resource group,
subscription, tenant and service-principal client ID in that staging
`clusterissuer.yaml`:

```bash
kubectl apply -k k8s/certificates/cloudflare/staging
# or
kubectl apply -k k8s/certificates/azure/staging

kubectl -n envoy-ai-gateway-system wait \
  --for=condition=Ready certificate/zer0-garden-wildcard-staging --timeout=10m
kubectl -n envoy-ai-gateway-system get secret zer0-garden-wildcard-staging-tls
```

Staging certificates are intentionally not browser-trusted. Once staging
issuance succeeds, set the same account/provider details in the selected
provider's `production/clusterissuer.yaml` and apply exactly one production
overlay:

```bash
kubectl apply -k k8s/certificates/cloudflare/production
# or
kubectl apply -k k8s/certificates/azure/production

kubectl -n envoy-ai-gateway-system wait \
  --for=condition=Ready certificate/zer0-garden-wildcard --timeout=10m
kubectl -n envoy-ai-gateway-system get secret zer0-garden-wildcard-tls
```

The Gateway manifest already defines the HTTPS listener and references
`envoy-ai-gateway-system/zer0-garden-wildcard-tls`. The Secret must exist in that
namespace; if your certificate uses another Secret name, update
`k8s/gateway-api/gateway.yaml` to match. Verify the listener reports
`Accepted=True` and `Programmed=True`, then check each HTTPRoute's parent status.

## Cloudflare DNS

Create a Cloudflare API Token scoped to the `zer0.garden` zone with only:

- `Zone / DNS / Edit`
- `Zone / Zone / Read`
- Zone Resources: `Include / Specific zone / zer0.garden`

Create the credential in cert-manager's cluster-resource namespace (this
installation uses `cert-manager`) without putting the token in Git:

```bash
read -r -s -p 'Cloudflare API token: ' CLOUDFLARE_API_TOKEN
printf '\n'
kubectl -n cert-manager create secret generic cloudflare-dns01 \
  --from-literal=api-token="$CLOUDFLARE_API_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -
unset CLOUDFLARE_API_TOKEN
```

If your shell or process inspection policy exposes command arguments, use your
cluster's external-secret manager instead of `--from-literal`. Keep the Secret
out of checked-in manifests and grant access only to cluster administrators and
cert-manager. The solver selects only the `zer0.garden` DNS zone.

## Azure DNS

The DNS zone must be an Azure DNS zone for `zer0.garden`; domain registration
with Azure alone is not sufficient. Create a dedicated service principal for
cert-manager and grant it **DNS Zone Contributor scoped to that DNS zone only**.
The checked-in Azure solver uses service-principal credentials so the manifests
also work on non-AKS Kubernetes.

Create `azure-dns01` in cert-manager's cluster-resource namespace with key
`client-secret`. Do this using an external-secret system where available; never
commit the credential:

```bash
read -r -s -p 'Azure DNS service-principal secret: ' AZURE_DNS_CLIENT_SECRET
printf '\n'
kubectl -n cert-manager create secret generic azure-dns01 \
  --from-literal=client-secret="$AZURE_DNS_CLIENT_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -
unset AZURE_DNS_CLIENT_SECRET
```

In the selected staging and production `clusterissuer.yaml`, replace
`CHANGE_ME@example.com`, `CHANGE_ME_DNS_RESOURCE_GROUP`,
`CHANGE_ME_SUBSCRIPTION_ID`, `CHANGE_ME_TENANT_ID`, and
`CHANGE_ME_SERVICE_PRINCIPAL_CLIENT_ID`. Ensure `hostedZoneName` is the Azure
DNS zone apex (`zer0.garden`). The client secret remains only in the Kubernetes
Secret. Workload Identity can avoid a stored client secret on AKS, but requires
AKS OIDC/Workload Identity enablement, a federated credential for the cert-manager
service account, and the cert-manager pod label; configure and verify those
cluster prerequisites before changing the issuer to managed identity.

## Troubleshooting And Renewal

Follow resource events rather than printing Secret data:

```bash
kubectl describe clusterissuer zer0-cloudflare-staging
kubectl -n envoy-ai-gateway-system describe certificate zer0-garden-wildcard-staging
kubectl -n envoy-ai-gateway-system get certificaterequest,order,challenge
kubectl -n cert-manager logs deploy/cert-manager --since=20m
```

For Azure, use `zer0-azure-staging` as the ClusterIssuer name. Confirm the
credential Secret is in `cert-manager`, the service principal has DNS Zone
Contributor on the correct zone resource ID, and DNS records are publicly
visible. A successful staging certificate proves DNS validation before using
the production ACME endpoint.

cert-manager renews the production certificate and updates the referenced Secret
automatically. Envoy Gateway watches the Gateway TLS Secret and should reload it
without a manual application restart. Confirm the certificate expiry and HTTPS
handshake after renewal. Let's Encrypt staging roots are intentionally not trusted
by normal clients; do not point the production Tunnel origin at the staging cert.

## Cloudflare Tunnel HTTPS Origin

After the Gateway's production HTTPS listener is programmed, configure
Cloudflared's origin service to use HTTPS port 443. With a wildcard-only
certificate, configure the TLS origin server name to a covered hostname, such as
`auth.zer0.garden`; this is only SNI/certificate validation. Preserve the original
HTTP `Host` header so Gateway routing continues to select `chat`, `grafana`,
`ai` or `auth`. Keep origin TLS verification enabled; do not use
`noTLSVerify: true` as a workaround. Cloudflared configuration is maintained in
the separate infrastructure repository, not by this stack.

Verify `chat.zer0.garden` and `auth.zer0.garden` over the Tunnel and directly
from the LAN before relying on the secure OIDC session cookie. Configure split
DNS for LAN clients and make sure they trust the certificate chain.

For an internal-only install without public DNS automation, use a certificate
from a CA trusted by the clients and Envoy Gateway. A self-signed certificate
requires explicitly installing/trusting that CA on Cloudflared and LAN clients;
it is not equivalent to a public ACME certificate.