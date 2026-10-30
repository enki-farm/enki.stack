from pathlib import Path

import pytest

from enkictl.checks import preflight
from enkictl.config import StackError
from enkictl.render import Deployment, resource

ROOT = Path(__file__).resolve().parents[2]


class Prerequisites:
    installation = "test-owned"
    server = "v1.34.0"
    client = "v1.34.1"
    allowed = True
    gpu = "0"
    cpu = "1"
    pods = []

    def __init__(self):
        self.calls = []

    def run(self, args):
        return "v4.3.1"

    def kubectl(self, *args):
        self.calls.append(args)
        if args[0] == "auth":
            return "yes" if self.allowed else "no"
        if args[:2] == ("get", "nodes"):
            return f"false\tTrue\t{self.cpu}\t2Gi\t{self.gpu}\n"
        if args[:2] == ("get", "storageclass"):
            return "storageclass/test\n"
        if args[:2] == ("get", "pods"):
            rows = []
            for pod in self.pods:
                metadata = pod.get("metadata", {})
                spec = pod.get("spec", {})
                status = pod.get("status", {})

                def resources(containers, init=False):
                    return ";".join(
                        ",".join(
                            ([container.get("restartPolicy", "")] if init else [])
                            + [
                                str(container.get("resources", {}).get("requests", {}).get(key, ""))
                                for key in ("cpu", "memory", "nvidia.com/gpu")
                            ]
                        )
                        for container in containers
                    )

                overhead = spec.get("overhead", {})
                rows.append(
                    "\t".join(
                        (
                            status.get("phase", ""),
                            metadata.get("labels", {}).get("enki.stack/installation", ""),
                            resources(spec.get("containers", [])),
                            resources(spec.get("initContainers", []), init=True),
                            ",".join(
                                str(overhead.get(key, ""))
                                for key in ("cpu", "memory", "nvidia.com/gpu")
                            ),
                            metadata.get("labels", {}).get("enki.stack/component", ""),
                        )
                    )
                )
            return "\n".join(rows)
        if args[:2] == ("get", "resourcequotas"):
            return ""
        return "ok"

    def json(self, *args):
        if args[0] == "version":
            return {
                "serverVersion": {"gitVersion": self.server},
                "clientVersion": {"gitVersion": self.client},
            }
        return {"items": []}


@pytest.fixture
def deployment(config):
    result = Deployment(config, ROOT, Prerequisites())
    result.render_charts = lambda _version: None
    return result


def test_bootstrap_gpu_is_warning_not_failure(deployment):
    assert any("GPU bootstrap" in warning for warning in preflight(deployment))


def test_preflight_reports_check_progress(deployment):
    progress = []
    preflight(deployment, progress.append)
    assert any("Versions passed:" in message for message in progress)
    assert "Kubernetes API is ready." in progress
    assert "Cluster-wide installation permissions passed." in progress
    assert any("Ready and schedulable" in message for message in progress)
    assert any("StorageClass" in message for message in progress)
    assert any("advertise GPUs" in message for message in progress)
    assert any("Capacity passed:" in message for message in progress)
    assert "ResourceQuota check passed: found 0." in progress


def test_preflight_requests_only_projected_cluster_data(deployment):
    preflight(deployment)
    calls = deployment.runner.calls

    node_call = next(call for call in calls if call[:2] == ("get", "nodes"))
    pod_call = next(call for call in calls if call[:2] == ("get", "pods"))
    storage_call = next(call for call in calls if call[:2] == ("get", "storageclass"))
    quota_call = next(call for call in calls if call[:2] == ("get", "resourcequotas"))
    pod_projection = next(arg for arg in pod_call if arg.startswith("jsonpath="))

    assert ".env" not in pod_projection
    assert ".resources.requests" in pod_projection
    assert any(arg.startswith("jsonpath=") for arg in node_call)
    assert storage_call[-2:] == ("-o", "name")
    assert quota_call[-2:] == ("-o", "name")


def test_existing_gpu_is_required(deployment):
    deployment.profile.gpu = "existing"
    with pytest.raises(StackError, match="advertise"):
        preflight(deployment)


def test_permission_failure_is_explicit(deployment):
    deployment.runner.allowed = False
    with pytest.raises(StackError, match="permission"):
        preflight(deployment)


def test_version_skew_blocks_before_render(deployment):
    deployment.runner.client = "v1.37.0"
    with pytest.raises(StackError, match="one minor"):
        preflight(deployment)


@pytest.mark.parametrize(("cpu_request", "passes"), [("1", True), ("1001m", False)])
def test_exact_cpu_capacity_threshold(deployment, cpu_request, passes):
    deployment.resources["auth"] = [
        resource(
            "Deployment",
            "test",
            spec={
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "resources": {"requests": {"cpu": cpu_request}},
                            }
                        ]
                    }
                },
            },
        )
    ]
    if passes:
        preflight(deployment)
    else:
        with pytest.raises(StackError, match="Insufficient aggregate cpu"):
            preflight(deployment)


@pytest.mark.parametrize(("owner", "label"), [("auth", "auth"), ("addon:searxng", "addon-searxng")])
def test_owned_pods_are_not_double_counted(deployment, owner, label):
    deployment.acting = [owner]
    deployment.resources["auth"] = [
        resource(
            "Deployment",
            "test",
            spec={
                "template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "1"}}}]}},
            },
        )
    ]
    deployment.runner.pods = [
        {
            "metadata": {
                "labels": {
                    "enki.stack/installation": "test-owned",
                    "enki.stack/component": label,
                }
            },
            "spec": {"containers": [{"resources": {"requests": {"cpu": "1"}}}]},
        }
    ]
    preflight(deployment)


def test_projected_pod_requests_reduce_available_capacity(deployment):
    deployment.runner.pods = [
        {
            "status": {"phase": "Running"},
            "spec": {"containers": [{"resources": {"requests": {"cpu": "250m"}}}]},
        }
    ]
    deployment.resources["auth"] = [
        resource(
            "Deployment",
            "test",
            spec={
                "template": {
                    "spec": {"containers": [{"resources": {"requests": {"cpu": "800m"}}}]}
                },
            },
        )
    ]
    with pytest.raises(StackError, match="Insufficient aggregate cpu"):
        preflight(deployment)


def test_unselected_owned_pods_still_consume_capacity(deployment):
    deployment.acting = ["monitoring"]
    deployment.runner.pods = [
        {
            "metadata": {
                "labels": {
                    "enki.stack/installation": "test-owned",
                    "enki.stack/component": "auth",
                }
            },
            "spec": {"containers": [{"resources": {"requests": {"cpu": "750m"}}}]},
        }
    ]
    deployment.resources["monitoring"] = [
        resource(
            "Deployment",
            "grafana",
            spec={
                "template": {
                    "spec": {
                        "containers": [{"resources": {"requests": {"cpu": "251m"}}}],
                    }
                }
            },
        )
    ]
    with pytest.raises(StackError, match="Insufficient aggregate cpu"):
        preflight(deployment)


def test_partial_controller_install_does_not_require_gpu_or_storage(deployment):
    deployment.full = False
    deployment.acting = ["kserve"]
    deployment.components = ["certificates", "ai-gateway", "kserve"]
    deployment.profile.gpu = "existing"
    warnings = preflight(deployment)
    assert not any(call[:2] == ("get", "storageclass") for call in deployment.runner.calls)
    assert any("Tempo is absent" in warning for warning in warnings)
