from __future__ import annotations

from typing import TYPE_CHECKING

from enkictl.components.base import Component, INSTALLATION_LABEL, Secret

if TYPE_CHECKING:
    from enkictl.lifecycle import Installation
    from enkictl.render import Deployment


def compose(deployment: Deployment) -> None:
    deployment.resources["warpgate-instance"] = deployment.template(
        "k8s/addons/warpgate/operator-rbac.yaml", {}
    ) + deployment.template(
        "k8s/addons/warpgate/warpgate.yaml",
        {
            "STORAGE_CLASS": deployment.profile.storage_class,
        },
    )


def credentials(deployment: Deployment) -> list[Secret]:
    return [
        Secret(
            "warpgate",
            "warpgate-admin",
            {"password": deployment.config.secrets.generated["warpgate-admin"]},
        )
    ]


def ready(installation: Installation, phase: str) -> None:
    if phase != "warpgate-instance":
        return
    resources = (
        ("warpgateinstance", "warpgate"),
        ("warpgateconnection", "warpgate-connection"),
        ("warpgaterole", "hermes-access"),
        ("warpgateuser", "hermes-access"),
        ("warpgatetarget", "hermes"),
        ("warpgateuserrole", "hermes-access"),
        ("warpgatetargetrole", "hermes-access"),
    )
    for kind, name in resources:
        installation.runner.kubectl(
            "wait",
            f"{kind}/{name}",
            "-n",
            "warpgate",
            "--for=condition=Ready",
            "--timeout=600s",
        )
    installation.runner.kubectl(
        "label",
        "secret",
        "hermes-access-password",
        "-n",
        "warpgate",
        f"{INSTALLATION_LABEL}={installation.state['id']}",
        "--overwrite",
    )


COMPONENT = Component(
    "warpgate",
    ("certificates",),
    ("warpgate-operator", "warpgate-instance"),
    ("warpgate", "warpgate-operator"),
    compose,
    ("helm/warpgate-operator", "k8s/addons/warpgate"),
    storage=True,
    generated_credentials=("warpgate-admin",),
    required_credentials=("warpgate-admin",),
    secret_names=(("warpgate", "warpgate-admin"),),
    credentials=credentials,
    ready=ready,
)