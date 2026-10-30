from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import bcrypt

from enkictl.components.base import Component, Secret
from enkictl.config import StackError

if TYPE_CHECKING:
    from enkictl.render import Deployment


def compose(d: Deployment) -> None:
    if d.profile.tls is None:
        raise StackError("kubeflow-hub requires tls configuration")
    d.resources["hub"] = d.load(f"k8s/kubeflow-hub/overlays/{d.profile.platform}", True)
    d.resources["hub"] += d.load("k8s/kubeflow-hub/jobs", True)
    d.resources["hub"] += d.load("k8s/kubeflow-hub/zot/inference-puller.yaml")
    d.resources["hub"] += d.template("k8s/dex/templates/hub-route.yaml", d.tokens())
    if d.profile.tls.mode == "self-signed":
        d.resources["hub"].append(
            d.resource(
                "Backend",
                "enki-dex",
                "kubeflow",
                spec={
                    "endpoints": [{"fqdn": {"hostname": f"auth.{d.profile.domain}", "port": 443}}],
                    "tls": {
                        "sni": f"auth.{d.profile.domain}",
                        "caCertificateRefs": [
                            {"group": "", "kind": "ConfigMap", "name": "enki-platform-trust"}
                        ],
                    },
                },
            )
        )
        policy = next(doc for doc in d.resources["hub"] if doc["kind"] == "SecurityPolicy")
        refs = [
            {"group": "gateway.envoyproxy.io", "kind": "Backend", "name": "enki-dex", "port": 443}
        ]
        policy["spec"]["oidc"]["provider"]["backendRefs"] = refs
        policy["spec"]["jwt"]["providers"][0]["remoteJWKS"]["backendRefs"] = refs
    for doc in d.resources["hub"]:
        if doc["kind"] == "Secret" or doc.get("roleRef", {}).get("name") == "cluster-admin":
            raise StackError("Unsafe Hub composition: inline Secret or cluster-admin binding")


def credentials(d: Deployment) -> list[Secret]:
    generated = d.config.secrets.generated
    result = [
        Secret("kubeflow", "hub-oidc", {"client-secret": generated["hub"]}),
        Secret(
            "kubeflow",
            "model-registry-db-secrets",
            {
                "MYSQL_USER_NAME": "root",
                "MYSQL_ROOT_PASSWORD": generated["mysql"],
            },
        ),
        Secret(
            "kubeflow",
            "model-catalog-postgres",
            {
                "POSTGRES_USER": "hub",
                "POSTGRES_PASSWORD": generated["postgres"],
            },
        ),
        Secret(
            "kubeflow",
            "zot-auth",
            {
                "hub-password": generated["zot-hub"],
                "kserve-password": generated["zot-kserve"],
                "htpasswd": "\n".join(
                    account
                    + ":"
                    + bcrypt.hashpw(generated["zot-" + account].encode(), bcrypt.gensalt()).decode()
                    for account in ("hub", "kserve")
                )
                + "\n",
            },
        ),
    ]
    for account, namespaces, name in [
        ("hub", ("kubeflow",), "zot-push"),
        ("kserve", ("kubeflow", "inference"), "zot-pull"),
    ]:
        docker = json.dumps(
            {
                "auths": {
                    f"zot.{d.profile.domain}": {
                        "auth": base64.b64encode(
                            f"{account}:{generated['zot-' + account]}".encode()
                        ).decode(),
                    }
                }
            }
        )
        result += [
            Secret(namespace, name, {".dockerconfigjson": docker}, "kubernetes.io/dockerconfigjson")
            for namespace in namespaces
        ]
    return result


COMPONENT = Component(
    "kubeflow-hub",
    ("auth",),
    ("hub",),
    ("kubeflow", "inference"),
    compose,
    ("k8s/kubeflow-hub/upstream", "k8s/kubeflow-hub/overlays", "k8s/dex/templates"),
    storage=True,
    generated_credentials=("mysql", "postgres", "zot-hub", "zot-kserve"),
    required_credentials=("hub", "mysql", "postgres", "zot-hub", "zot-kserve"),
    secret_names=tuple(
        ("kubeflow", name)
        for name in (
            "hub-oidc",
            "model-registry-db-secrets",
            "model-catalog-postgres",
            "zot-auth",
            "zot-push",
            "zot-pull",
        )
    )
    + (("inference", "zot-pull"),),
    credentials=credentials,
)
