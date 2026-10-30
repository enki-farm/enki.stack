from __future__ import annotations

from typing import TYPE_CHECKING

from enkictl.components.base import Component

if TYPE_CHECKING:
    from enkictl.lifecycle import Installation
    from enkictl.render import Deployment


def compose(d: Deployment) -> None:
    d.resources["gateway-controller"] = d.load("k8s/gateway-api/deployment.yaml")
    d.resources["gateway"] = [
        doc
        for name in ("gatewayclass", "gateway", "client-traffic-policy", "envoy-proxy")
        for doc in d.load(f"k8s/gateway-api/{name}.yaml")
    ]
    gateway = next(doc for doc in d.resources["gateway"] if doc["kind"] == "Gateway")
    gateway["spec"]["listeners"][0]["tls"]["certificateRefs"][0]["name"] = "enki-platform-tls"


def ready(i: Installation, phase: str) -> None:
    if phase == "gateway":
        i.runner.kubectl(
            "wait",
            "gateway/envoy-ai-gateway-basic",
            "-n",
            i.deployment.gateway_namespace,
            "--for=condition=Programmed",
            "--timeout=300s",
        )


COMPONENT = Component(
    "ai-gateway",
    ("certificates",),
    ("gateway-controller", "gateway"),
    ("envoy-gateway-system", "envoy-ai-gateway-system"),
    compose,
    ("helm/gateway", "k8s/gateway-api"),
    ready=ready,
)
