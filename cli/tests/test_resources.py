from decimal import Decimal
from pathlib import Path

import pytest

from enkictl.config import read_yaml
from enkictl.postrender import mark
from enkictl.render import documents
from enkictl.tools import Runner

ROOT = Path(__file__).resolve().parents[2]
SCALES = {"m": Decimal("0.001"), "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}


def quantity(value):
    text = str(value)
    for suffix, scale in SCALES.items():
        if text.endswith(suffix):
            return Decimal(text[: -len(suffix)]) * scale
    return Decimal(text)


def assert_resources(resources, source):
    for name in ("cpu", "memory"):
        request = quantity(resources.get("requests", {}).get(name, 0))
        limit = quantity(resources.get("limits", {}).get(name, 0))
        assert 0 < request <= limit, f"{source}: invalid/missing {name} request/limit"


def assert_container_resources(value, source, defaults=None):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"containers", "initContainers"} and isinstance(child, list):
                for container in child:
                    # Strategic merge patches may deliberately specify only one resource field.
                    if "image" in container:
                        resources = container.get("resources", {})
                        if defaults:
                            resources = {
                                kind: {**budget, **resources.get(kind, {})}
                                for kind, budget in defaults.items()
                            }
                        assert_resources(resources, f"{source}/{container['name']}")
            assert_container_resources(child, source, defaults)
    elif isinstance(value, list):
        for child in value:
            assert_container_resources(child, source, defaults)


def test_authored_workload_resources():
    for directory in (
        "k8s/addons",
        "k8s/aks",
        "k8s/cert-manager",
        "k8s/dex",
        "k8s/monitoring",
        "models",
    ):
        for path in (ROOT / directory).rglob("*.yaml"):
            if path == ROOT / "k8s/dex/config.base.yaml":
                continue
            assert_container_resources(documents(path.read_text()), path.relative_to(ROOT))
    for path in (ROOT / "models").rglob("*.yaml"):
        for doc in documents(path.read_text()):
            if doc.get("kind") == "InferenceService":
                model = doc["spec"]["predictor"].get("model")
                if model is not None:
                    assert_resources(model.get("resources", {}), path)


@pytest.mark.parametrize("platform", ["dgx-spark", "aks"])
def test_rendered_hub_resources(platform):
    docs = documents(Runner("never-contact").render(ROOT / f"k8s/kubeflow-hub/overlays/{platform}"))
    assert_container_resources(docs, platform)
    initializer = next(doc for doc in docs if doc["kind"] == "ClusterStorageContainer")
    assert_resources(initializer["spec"]["container"]["resources"], platform)


def test_generated_sidecar_resource_configuration(config):
    from enkictl.render import Deployment

    deployment = Deployment(config, ROOT, Runner("never-contact"), ["ai-gateway", "gpu-operator"])
    deployment.compose()
    gateway_docs = deployment.resources["gateway"]
    gateway = next(doc for doc in gateway_docs if doc["kind"] == "Gateway")
    settings = next(doc for doc in gateway_docs if doc["kind"] == "GatewayConfig")
    assert (
        gateway["metadata"]["annotations"]["aigateway.envoyproxy.io/gateway-config"]
        == (settings["metadata"]["name"])
    )
    assert_resources(settings["spec"]["extProc"]["kubernetes"]["resources"], "extproc")
    proxy = next(doc for doc in gateway_docs if doc["kind"] == "EnvoyProxy")
    envoy = proxy["spec"]["provider"]["kubernetes"]["envoyDeployment"]
    assert_resources(envoy["container"]["resources"], "envoy")
    shutdown = envoy["patch"]["value"]["spec"]["template"]["spec"]["containers"][0]
    assert shutdown["name"] == "shutdown-manager"
    assert_resources(shutdown["resources"], "shutdown-manager")
    defaults = deployment.resources["gpu"][0]["spec"]["limits"][0]
    assert_resources(
        {"requests": defaults["defaultRequest"], "limits": defaults["default"]}, "gpu defaults"
    )
    values = read_yaml(ROOT / "helm/gpu-operator/values-gpu-operator.yaml")
    for component in (
        "toolkit",
        "devicePlugin",
        "gfd",
        "dcgmExporter",
        "nodeStatusExporter",
        "validator",
    ):
        assert_resources(values[component]["resources"], component)


def test_helm_test_pod_defaults_preserve_explicit_resources():
    pod = {
        "kind": "Pod",
        "metadata": {"annotations": {"helm.sh/hook": "test"}},
        "spec": {
            "containers": [{"name": "wget", "image": "busybox"}],
            "initContainers": [
                {
                    "name": "setup",
                    "image": "busybox",
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "64Mi"},
                        "limits": {"cpu": "1", "memory": "256Mi"},
                    },
                }
            ],
        },
    }
    original = dict(pod["spec"]["initContainers"][0]["resources"])
    result = mark(pod, "test")
    assert_container_resources(result, "test-hook")
    assert result["spec"]["initContainers"][0]["resources"] == original


@pytest.mark.parametrize(
    "resources",
    [
        {"requests": {"cpu": "2"}},
        {"limits": {"cpu": "10m", "memory": "16Mi"}},
    ],
)
def test_helm_test_pod_partial_budgets_remain_valid(resources):
    original = {kind: dict(budget) for kind, budget in resources.items()}
    pod = {
        "kind": "Pod",
        "metadata": {"annotations": {"helm.sh/hook": "test"}},
        "spec": {"containers": [{"name": "wget", "image": "busybox", "resources": resources}]},
    }
    result = mark(pod, "test")
    assert_container_resources(result, "test-hook")
    for kind, budget in original.items():
        for name, value in budget.items():
            assert result["spec"]["containers"][0]["resources"][kind][name] == value
