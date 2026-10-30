import copy
import io
import re
from pathlib import Path

import pytest

from enkictl.config import Profile, dump
from enkictl.postrender import LABEL, mark
from enkictl.postrender import main as postrender
from enkictl.render import Deployment, documents, resource
from enkictl.tools import Runner

ROOT = Path(__file__).resolve().parents[2]


def test_mark_preserves_kserve_podspec():
    template = {"containers": [{"name": "worker", "image": "example/worker:test"}]}
    doc = resource(
        "LLMInferenceServiceConfig",
        "worker",
        "kserve",
        apiVersion="serving.kserve.io/v1alpha2",
        spec={"template": copy.deepcopy(template)},
    )

    marked = mark(doc, "installation", "kserve")

    assert marked["spec"]["template"] == template
    assert marked["metadata"]["labels"][LABEL] == "installation"
    assert marked["metadata"]["labels"]["enki.stack/component"] == "kserve"


@pytest.mark.parametrize(
    ("cpu", "expected"), [(6, "6"), ("4000m", "4"), ("28000m", "28"), ("256m", "256m"), ("4", "4")]
)
def test_mark_normalizes_llm_scheduler_cpu(cpu, expected):
    resources = {"requests": {"cpu": cpu, "memory": "2Gi"}, "limits": {"cpu": cpu}}
    doc = resource(
        "LLMInferenceServiceConfig",
        "scheduler",
        spec={
            "router": {
                "scheduler": {
                    "template": {"containers": [{"name": "main", "resources": resources}]}
                }
            }
        },
    )

    mark(doc, "installation", "kserve")

    assert resources == {
        "requests": {"cpu": expected, "memory": "2Gi"},
        "limits": {"cpu": expected},
    }


@pytest.mark.parametrize(
    "kind", ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job"]
)
def test_mark_labels_workload_podtemplates(kind):
    doc = resource(
        kind,
        "worker",
        spec={"template": {"metadata": {"labels": {"existing": "label"}}, "spec": {}}},
    )

    marked = mark(doc, "installation", "kserve")

    assert marked["spec"]["template"]["metadata"]["labels"] == {
        "existing": "label",
        LABEL: "installation",
        "enki.stack/component": "kserve",
    }


@pytest.mark.parametrize("name", ["librechat", "searxng", "crawl4ai"])
def test_addon_ownership_uses_valid_kubernetes_labels(name):
    doc = resource("Deployment", name, name, spec={"template": {"spec": {"containers": []}}})
    marked = mark(doc, "installation", f"addon:{name}")
    for metadata in (marked["metadata"], marked["spec"]["template"]["metadata"]):
        value = metadata["labels"]["enki.stack/component"]
        assert value == f"addon-{name}"
        assert len(value) <= 63
        assert re.fullmatch(r"[a-zA-Z0-9]([a-zA-Z0-9_.-]*[a-zA-Z0-9])?", value)


@pytest.mark.parametrize("excluded", ["", "shared.example.test"])
def test_postrenderer_excludes_only_selected_crds(monkeypatch, capsys, excluded):
    docs = [
        resource("CustomResourceDefinition", "shared.example.test"),
        resource("CustomResourceDefinition", "unique.example.test"),
        resource("ConfigMap", "shared.example.test", "kserve"),
    ]
    monkeypatch.setenv("ENKI_INSTALLATION_ID", "test-render-only")
    monkeypatch.setenv("ENKI_HELM_EXCLUDE_CRDS", excluded)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n---\n".join(dump(doc) for doc in docs)))

    postrender()

    actual = documents(capsys.readouterr().out)
    assert [(doc["kind"], doc["metadata"]["name"]) for doc in actual] == [
        (doc["kind"], doc["metadata"]["name"])
        for doc in docs
        if not (doc["kind"] == "CustomResourceDefinition" and doc["metadata"]["name"] == excluded)
    ]


@pytest.mark.parametrize("platform", ["dgx-spark", "aks"])
@pytest.mark.parametrize("mode", ["self-signed", "azure", "cloudflare"])
def test_real_kustomize_composition(config, platform, mode):
    data = config.profile.model_dump()
    data.update(platform=platform, gpu="operator" if platform == "dgx-spark" else "existing")
    if mode != "self-signed":
        data["tls"] = {
            "mode": mode,
            "acme_email": "admin@platform.internal",
            "dns_zone": "platform.internal",
        }
        if mode == "azure":
            data["tls"]["azure"] = {
                "subscription_id": "11111111-1111-1111-1111-111111111111",
                "tenant_id": "22222222-2222-2222-2222-222222222222",
                "client_id": "33333333-3333-3333-3333-333333333333",
                "resource_group": "dns",
            }
    config.profile = Profile.model_validate(data)
    deployment = Deployment(config, ROOT, Runner(config.profile.context))
    deployment.compose()
    text = dump(deployment.resources)
    assert "zer0.garden" not in text
    assert "enki-farm:core" not in text
    assert "@DEX_" not in text
    assert "never-print-me" not in text
    assert "kind: Certificate\n" in text
    clients = next(
        doc
        for doc in deployment.resources["auth"]
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "dex-config"
    )
    dex = documents(clients["data"]["config.yaml"])[0]
    client = next(client for client in dex["staticClients"] if client["id"] == "librechat")
    assert client["redirectURIs"] == ["https://chat.platform.internal/oauth/openid/callback"]
    assert client["secretEnv"] == "LIBRECHAT_CLIENT_SECRET"
    assert not any(
        doc["metadata"].get("namespace") == "librechat"
        for docs in deployment.resources.values()
        for doc in docs
    )
    hub = deployment.resources["hub"]
    assert any(doc["kind"] == "SecurityPolicy" for doc in hub)
    pvcs = [doc for doc in hub if doc["kind"] == "PersistentVolumeClaim"]
    assert pvcs and all(
        doc["spec"]["storageClassName"] == config.profile.storage_class for doc in pvcs
    )
    issuer = deployment.resources["certificate"][0]
    if mode == "self-signed":
        assert issuer["spec"] == {"selfSigned": {}}
        assert deployment.resources["trust"][0]["kind"] == "Bundle"
        assert "insecureSkipVerify" not in text
    else:
        assert "acme-v02.api.letsencrypt.org" in issuer["spec"]["acme"]["server"]
        assert len(issuer["spec"]["acme"]["solvers"]) == 1
