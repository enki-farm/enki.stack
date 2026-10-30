from __future__ import annotations

from typing import TYPE_CHECKING

from enkictl.components.base import Component, Secret
from enkictl.config import StackError

if TYPE_CHECKING:
    from enkictl.render import Deployment


def compose(d: Deployment) -> None:
    p = d.profile
    if p.auth is None or p.tls is None:
        raise StackError("monitoring requires auth and tls configuration")
    roles = f"(contains(groups[*], '{p.auth.grafana_viewer_group}') && 'Viewer')"
    if p.auth.grafana_admin_group:
        roles = f"(contains(groups[*], '{p.auth.grafana_admin_group}') && 'Admin') || {roles}"
    d.overrides["grafana"] = d.template(
        "k8s/dex/templates/grafana-values.yaml", {**d.tokens(), "GRAFANA_ROLE_PATH": roles}
    )[0]
    d.overrides["kube-prometheus-stack"] = {
        "prometheusOperator": {"admissionWebhooks": {"certManager": {"enabled": True}}},
    }
    d.resources["content"] = [
        doc for doc in d.load("k8s/monitoring", True) if doc["kind"] != "HTTPRoute"
    ]
    d.resources["content"] += d.template("k8s/dex/templates/grafana-route.yaml", d.tokens())
    d.resources["content"] += d.load("k8s/monitoring/grafana-plugin-provisioning.yaml")
    d.resources["content"] += d.load("k8s/monitoring/dashboards", True)
    if p.tls.mode == "self-signed":
        d.overrides["enki-trust-reloader"] = {
            "reloader": {
                "watchGlobally": False,
                "ignoreSecrets": True,
                "reloadStrategy": "annotations",
            }
        }
        d.overrides["grafana"]["extraConfigmapMounts"] = [
            {
                "name": "plugin-provisioning",
                "mountPath": "/etc/grafana/provisioning/plugins",
                "configMap": "grafana-plugin-provisioning",
                "readOnly": True,
            },
            {
                "name": "platform-trust",
                "mountPath": "/etc/enki-trust",
                "configMap": "enki-platform-trust",
                "readOnly": True,
            },
        ]
        d.overrides["grafana"]["grafana.ini"]["auth.generic_oauth"]["tls_client_ca"] = (
            "/etc/enki-trust/ca.crt"
        )
        d.overrides["grafana"]["annotations"] = {
            "configmap.reloader.stakater.com/reload": "enki-platform-trust",
        }


def credentials(d: Deployment) -> list[Secret]:
    generated = d.config.secrets.generated
    return [
        Secret("observability", "oidc-credentials", {"client-secret": generated["grafana"]}),
        Secret(
            "observability",
            "grafana-admin",
            {
                "admin-user": "admin",
                "admin-password": generated["grafana-admin"],
            },
        ),
    ]


COMPONENT = Component(
    "monitoring",
    ("auth",),
    ("monitoring", "content"),
    ("observability",),
    compose,
    ("helm/monitoring", "k8s/monitoring", "k8s/dex/templates"),
    storage=True,
    generated_credentials=("grafana-admin",),
    required_credentials=("grafana", "grafana-admin"),
    secret_names=(("observability", "oidc-credentials"), ("observability", "grafana-admin")),
    credentials=credentials,
)
