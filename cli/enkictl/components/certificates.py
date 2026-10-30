from __future__ import annotations

from typing import TYPE_CHECKING

from enkictl.components.base import Component
from enkictl.config import ACMETLS, StackError, private_write

if TYPE_CHECKING:
    from enkictl.lifecycle import Installation
    from enkictl.render import Deployment


def compose(d: Deployment) -> None:
    if d.profile.tls is None:
        raise StackError("certificates requires tls configuration")
    d.resources["cert-manager"] = d.load("k8s/cert-manager/deployment.yaml")
    tls = d.profile.tls
    spec = {"selfSigned": {}}
    if isinstance(tls, ACMETLS):
        if tls.mode == "azure":
            azure = tls.azure
            if azure is None:
                raise StackError("Azure DNS requires azure configuration")
            solver = {
                "azureDNS": {
                    "environment": "AzurePublicCloud",
                    "hostedZoneName": tls.dns_zone,
                    "resourceGroupName": azure.resource_group,
                    "subscriptionID": str(azure.subscription_id),
                    "tenantID": str(azure.tenant_id),
                    "clientID": str(azure.client_id),
                    "clientSecretSecretRef": {"name": "enki-dns01", "key": "client-secret"},
                }
            }
        else:
            solver = {
                "cloudflare": {
                    "apiTokenSecretRef": {"name": "enki-dns01", "key": "api-token"},
                }
            }
        spec = {
            "acme": {
                "email": tls.acme_email,
                "server": tls.server,
                "privateKeySecretRef": {"name": f"enki-acme-{tls.environment}"},
                "solvers": [{"dns01": solver, "selector": {"dnsZones": [tls.dns_zone]}}],
            }
        }
    d.resources["certificate"] = [
        d.resource("ClusterIssuer", d.issuer_name, spec=spec),
        d.resource(
            "Certificate",
            "enki-platform",
            d.gateway_namespace,
            spec={
                "secretName": "enki-platform-tls",
                "issuerRef": {"name": d.issuer_name, "kind": "ClusterIssuer"},
                "privateKey": {"rotationPolicy": "Never"},
                "dnsNames": [d.profile.domain, f"*.{d.profile.domain}"],
            },
        ),
    ]
    if d.profile.tls.mode == "self-signed":
        d.overrides["trust-manager"] = {"app": {"trust": {"namespace": d.gateway_namespace}}}
        d.resources["trust"] = [
            d.resource(
                "Bundle",
                "enki-platform-trust",
                spec={
                    "sources": [
                        {"useDefaultCAs": True},
                        {"secret": {"name": "enki-platform-tls", "key": "tls.crt"}},
                    ],
                    "target": {
                        "configMap": {"key": "ca.crt"},
                        "namespaceSelector": {"matchLabels": {"enki.stack/trust": "true"}},
                    },
                },
            )
        ]


def ready(i: Installation, phase: str) -> None:
    if phase == "cert-manager":
        for name in ("cert-manager", "cert-manager-webhook", "cert-manager-cainjector"):
            i.runner.kubectl(
                "rollout",
                "status",
                "deployment/" + name,
                "-n",
                "cert-manager",
                "--timeout=300s",
            )
    elif phase == "certificate":
        from enkictl.checks import certificate

        i.runner.kubectl(
            "wait",
            "clusterissuer/" + i.deployment.issuer_name,
            "--for=condition=Ready",
            "--timeout=600s",
        )
        for namespace, name in i.generated_secret_names(include_charts=False):
            if name.startswith("enki-acme-"):
                i.register_generated_secret(namespace, name)
        i.runner.kubectl(
            "wait",
            "certificate/enki-platform",
            "-n",
            i.deployment.gateway_namespace,
            "--for=condition=Ready",
            "--timeout=600s",
        )
        for namespace, name in i.generated_secret_names(include_charts=False):
            if name != "enki-dns01":
                i.register_generated_secret(namespace, name)
        if not i.config.profile.domain:
            raise StackError("Certificate export requires a domain")
        public = certificate(
            i.runner.get("secret", "enki-platform-tls", i.deployment.gateway_namespace),
            i.config.profile.domain,
        )
        private_write(i.path.parent / "platform-certificate.crt", public)
    elif phase == "trust" and getattr(i.config.profile.tls, "mode", None) == "self-signed":
        i.runner.kubectl(
            "wait",
            "bundle/enki-platform-trust",
            "--for=condition=Synced",
            "--timeout=300s",
        )


COMPONENT = Component(
    "certificates",
    (),
    ("cert-manager", "certificate", "trust"),
    ("cert-manager", "envoy-ai-gateway-system"),
    compose,
    ("k8s/cert-manager",),
    ready=ready,
)
