from __future__ import annotations

from typing import TYPE_CHECKING

from enkictl.components.base import Component

if TYPE_CHECKING:
    from enkictl.lifecycle import Installation
    from enkictl.render import Deployment


def compose(d: Deployment) -> None:
    d.resources["gpu"] = d.load("k8s/gpu-operator/resource-defaults.yaml")


def ready(i: Installation, phase: str) -> None:
    i.runner.kubectl(
        "rollout",
        "status",
        "daemonset/nvidia-device-plugin-daemonset",
        "-n",
        "gpu-operator",
        "--timeout=600s",
    )


COMPONENT = Component(
    "gpu-operator",
    (),
    ("gpu",),
    ("gpu-operator",),
    compose,
    ("helm/gpu-operator", "k8s/gpu-operator"),
    ready=ready,
)
