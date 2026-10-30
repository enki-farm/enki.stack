from __future__ import annotations

import copy
import hashlib
from typing import TYPE_CHECKING

from enkictl.components.base import Component, Secret
from enkictl.config import dump

if TYPE_CHECKING:
    from enkictl.render import Deployment


def compose(d: Deployment) -> None:
    p = d.profile
    d.resources["auth"] = d.load("k8s/dex", True)
    next(doc for doc in d.resources["auth"] if doc["kind"] == "PersistentVolumeClaim")["spec"][
        "storageClassName"
    ] = p.storage_class
    dex = d.template(
        "k8s/dex/config.base.yaml", {**d.tokens(), "CHAT_URL": "", "ADMIN_CALLBACK": ""}
    )[0]
    dex["staticClients"] = [
        {
            "id": service,
            "name": service.title(),
            "secretEnv": f"{service.upper()}_CLIENT_SECRET",
            "redirectURIs": [p.origin(host) + callback],
        }
        for service, host, callback in (
            ("grafana", "grafana", "/login/generic_oauth"),
            ("hub", "hub", "/oauth2/callback"),
            ("librechat", "chat", "/oauth/openid/callback"),
        )
    ]
    connector = copy.deepcopy(d.config.connector)
    dex.update(d.documents(dump(connector).replace("@DEX_ISSUER@", p.origin("auth")))[0])
    d.resources["auth"].insert(
        0, d.resource("ConfigMap", "dex-config", "dex", data={"config.yaml": dump(dex)})
    )
    deployment = next(doc for doc in d.resources["auth"] if doc["kind"] == "Deployment")
    deployment["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
        "checksum/dex-config"
    ] = hashlib.sha256(dump(dex).encode()).hexdigest()
    d.resources["auth"] += d.template("k8s/dex/templates/dex-route.yaml", d.tokens())


def credentials(d: Deployment) -> list[Secret]:
    generated = d.config.secrets.generated
    return [
        Secret(
            "dex",
            "dex-secrets",
            {
                **d.config.secrets.connector,
                "GRAFANA_CLIENT_SECRET": generated["grafana"],
                "HUB_CLIENT_SECRET": generated["hub"],
                "LIBRECHAT_CLIENT_SECRET": generated["librechat"],
            },
        )
    ]


COMPONENT = Component(
    "auth",
    ("ai-gateway",),
    ("auth",),
    ("dex",),
    compose,
    ("k8s/dex",),
    storage=True,
    generated_credentials=("grafana", "hub", "librechat"),
    required_credentials=("grafana", "hub", "librechat"),
    secret_names=(("dex", "dex-secrets"),),
    credentials=credentials,
)
