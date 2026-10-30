from __future__ import annotations

import base64
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from enkictl.components import REGISTRY
from enkictl.components.base import COMPONENT_LABEL
from enkictl.config import ACMETLS, StackError
from enkictl.postrender import LABEL, component_label
from enkictl.render import Deployment

_NODE_PREFLIGHT_TEMPLATE = (
    r'{range .items[*]}{.spec.unschedulable}{"\t"}'
    r'{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\t"}'
    r'{.status.allocatable.cpu}{"\t"}{.status.allocatable.memory}{"\t"}'
    r'{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
)
_POD_PREFLIGHT_TEMPLATE = (
    r'{range .items[*]}{.status.phase}{"\t"}'
    r'{.metadata.labels.enki\.stack/installation}{"\t"}'
    r'{range .spec.containers[*]}{.resources.requests.cpu}{","}'
    r'{.resources.requests.memory}{","}{.resources.requests.nvidia\.com/gpu}{";"}{end}{"\t"}'
    r'{range .spec.initContainers[*]}{.restartPolicy}{","}'
    r'{.resources.requests.cpu}{","}{.resources.requests.memory}{","}'
    r'{.resources.requests.nvidia\.com/gpu}{";"}{end}{"\t"}'
    r'{.spec.overhead.cpu}{","}{.spec.overhead.memory}{","}'
    r'{.spec.overhead.nvidia\.com/gpu}{"\t"}'
    r'{.metadata.labels.enki\.stack/component}{"\n"}{end}'
)


def quantity(value: str | int | float) -> Decimal:
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)", str(value))
    scales = {
        "": 1,
        "n": Decimal("1e-9"),
        "u": Decimal("1e-6"),
        "m": Decimal(".001"),
        "k": 1000,
        "K": 1000,
        "M": 10**6,
        "G": 10**9,
        "T": 10**12,
        "P": 10**15,
        "E": 10**18,
        **{f"{s}i": 1024**i for i, s in enumerate("KMGTPE", 1)},
    }
    if not match or match[2] not in scales:
        raise StackError(f"Unsupported Kubernetes quantity: {value}")
    return Decimal(match[1]) * scales[match[2]]


def requests(spec: dict) -> dict[str, Decimal]:
    result = {}
    for name in ("cpu", "memory", "nvidia.com/gpu"):
        normal = sum(
            (
                quantity(c.get("resources", {}).get("requests", {}).get(name, 0))
                for c in spec.get("containers", [])
            ),
            Decimal(0),
        )
        # Restartable init sidecars contribute to steady-state and later init peaks.
        sidecars = Decimal(0)
        peak = Decimal(0)
        for container in spec.get("initContainers", []):
            amount = quantity(container.get("resources", {}).get("requests", {}).get(name, 0))
            if container.get("restartPolicy") == "Always":
                sidecars += amount
                peak = max(peak, sidecars)
            else:
                peak = max(peak, sidecars + amount)
        result[name] = max(normal + sidecars, peak) + quantity(
            spec.get("overhead", {}).get(name, 0)
        )
    return result


def _present(value: str) -> str:
    return "" if value in {"<no value>", "<nil>"} else value


def _parse_node_inventory(output: str) -> list[dict]:
    nodes = []
    for line in output.splitlines():
        if not line:
            continue
        fields = [_present(value) for value in line.split("\t")]
        if len(fields) != 5:
            raise StackError("kubectl returned invalid preflight node data")
        unschedulable, ready, cpu, memory, gpu = fields
        allocatable = {
            key: value
            for key, value in (("cpu", cpu), ("memory", memory), ("nvidia.com/gpu", gpu))
            if value
        }
        nodes.append(
            {
                "spec": {"unschedulable": unschedulable.lower() == "true"},
                "status": {
                    "conditions": [{"type": "Ready", "status": ready}] if ready else [],
                    "allocatable": allocatable,
                },
            }
        )
    return nodes


def _parse_container_requests(value: str, *, init: bool = False) -> list[dict]:
    containers = []
    for container in value.split(";"):
        if not container:
            continue
        fields = [_present(field) for field in container.split(",")]
        expected = 4 if init else 3
        if len(fields) != expected:
            raise StackError("kubectl returned invalid preflight pod resource data")
        restart_policy = fields.pop(0) if init else ""
        names = ("cpu", "memory", "nvidia.com/gpu")
        containers.append(
            {
                **({"restartPolicy": restart_policy} if restart_policy else {}),
                "resources": {
                    "requests": {
                        name: amount for name, amount in zip(names, fields, strict=True) if amount
                    }
                },
            }
        )
    return containers


def _parse_pod_inventory(output: str) -> list[dict]:
    pods = []
    for line in output.splitlines():
        if not line:
            continue
        fields = [_present(value) for value in line.split("\t")]
        if len(fields) != 6:
            raise StackError("kubectl returned invalid preflight pod data")
        phase, installation, containers, init_containers, overhead, component = fields
        overhead_values = [_present(value) for value in overhead.split(",")]
        if len(overhead_values) != 3:
            raise StackError("kubectl returned invalid preflight pod resource data")
        spec = {
            "containers": _parse_container_requests(containers),
            "initContainers": _parse_container_requests(init_containers, init=True),
            "overhead": {
                name: amount
                for name, amount in zip(
                    ("cpu", "memory", "nvidia.com/gpu"), overhead_values, strict=True
                )
                if amount
            },
        }
        pods.append(
            {
                "status": {"phase": phase},
                "metadata": {"labels": {LABEL: installation, COMPONENT_LABEL: component}},
                "spec": spec,
            }
        )
    return pods


def version(value: str) -> tuple[int, int]:
    match = re.match(r"v?(\d+)\.(\d+)", value)
    if not match:
        raise StackError(f"Unrecognized Kubernetes version: {value}")
    return int(match[1]), int(match[2])


def certificate(secret: dict, domain: str) -> str:
    try:
        cert_bytes = base64.b64decode(secret["data"]["tls.crt"], validate=True)
        cert = x509.load_pem_x509_certificate(cert_bytes)
        key = load_pem_private_key(base64.b64decode(secret["data"]["tls.key"]), password=None)
        if cert.public_key() != key.public_key():
            raise ValueError("certificate key mismatch")
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if not {domain, f"*.{domain}"}.issubset(set(names.get_values_for_type(x509.DNSName))):
            raise ValueError("hostname coverage mismatch")
        if not cert.not_valid_before_utc <= datetime.now(UTC) < cert.not_valid_after_utc:
            raise ValueError("certificate not currently valid")
        if secret.get("type") != "kubernetes.io/tls":
            raise ValueError("incorrect Secret type")
        return cert_bytes.decode()
    except (ValueError, KeyError, x509.ExtensionNotFound, TypeError):
        raise StackError(
            "Platform TLS Secret has invalid type, key, validity or DNS coverage"
        ) from None


def preflight(deployment: Deployment, progress: Callable[[str], None] | None = None) -> list[str]:
    p, runner = deployment.profile, deployment.runner

    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    report("Checking Helm and Kubernetes versions...")
    helm_version = runner.run(["helm", "version", "--short"]).strip()
    info = runner.json("version")
    server = version(info["serverVersion"]["gitVersion"])
    client = version(info["clientVersion"]["gitVersion"])
    # Rendered Helm templates carry kubeVersion checks; the shared baseline is
    # Kubernetes 1.31, required by the pinned Gateway API v1.6 CRDs.
    if "ai-gateway" in deployment.components and server < (1, 31):
        raise StackError("Pinned platform APIs require Kubernetes 1.31 or newer")
    if server[0] != client[0] or abs(server[1] - client[1]) > 1:
        raise StackError("kubectl must be within one minor version of the Kubernetes server")
    report(
        f"Versions passed: Helm {helm_version}; Kubernetes server "
        f"{info['serverVersion']['gitVersion']}; kubectl client "
        f"{info['clientVersion']['gitVersion']}."
    )
    report("Rendering Helm charts...")
    deployment.render_charts(info["serverVersion"]["gitVersion"])
    report("Helm chart rendering passed.")
    report("Checking Kubernetes API readiness...")
    runner.kubectl("get", "--raw=/readyz")
    report("Kubernetes API is ready.")
    # Installation manages cluster-wide RBAC/controllers. Do not substitute
    # namespace-local checks that miss bind/escalate/CRD capabilities.
    report("Checking cluster-wide installation permissions...")
    for verb, target in [
        ("get", "nodes"),
        ("get", "secrets"),
        ("list", "pods"),
        ("create", "*"),
        ("patch", "*"),
        ("update", "*"),
        ("delete", "*"),
        ("bind", "clusterroles.rbac.authorization.k8s.io"),
        ("escalate", "clusterroles.rbac.authorization.k8s.io"),
    ]:
        allowed = runner.kubectl("auth", "can-i", verb, target, "--all-namespaces").strip()
        if allowed != "yes":
            raise StackError(f"Missing cluster installation permission: {verb} {target}")
    report("Cluster-wide installation permissions passed.")
    report("Reading node inventory...")
    node_output = runner.kubectl("get", "nodes", "-o", f"jsonpath={_NODE_PREFLIGHT_TEMPLATE}")
    nodes = _parse_node_inventory(node_output)
    ready = [
        node
        for node in nodes
        if not node.get("spec", {}).get("unschedulable")
        and any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in node["status"].get("conditions", [])
        )
    ]
    if not ready:
        raise StackError("No Ready schedulable nodes")
    if len(ready) != len(nodes):
        raise StackError("All cluster nodes must be Ready and schedulable for installation")
    report(f"Nodes passed: {len(ready)} Ready and schedulable.")
    if any(REGISTRY[name].storage for name in deployment.acting if name in REGISTRY):
        report(f"Checking configured StorageClass {p.storage_class}...")
        runner.kubectl("get", "storageclass", p.storage_class, "-o", "name")
        report(f"StorageClass {p.storage_class} exists.")
    gpu_nodes = [
        n
        for n in ready
        if quantity(n["status"].get("allocatable", {}).get("nvidia.com/gpu", 0)) > 0
    ]
    if deployment.full and p.gpu == "existing" and not gpu_nodes:
        raise StackError("Existing GPU management must advertise allocatable nvidia.com/gpu")
    report(f"{len(gpu_nodes)} Ready node(s) advertise GPUs.")
    warnings = [
        "Physical free disk, LoadBalancer reachability, DNS from Pods and host GPU drivers "
        "cannot be proven remotely. Verify these before relying on the platform.",
        "Capacity is an aggregate estimate, not proof of per-node scheduling or image architecture "
        "compatibility; inspect affinity, taints and quotas.",
    ]
    if "gpu-operator" in deployment.acting and not gpu_nodes:
        warnings.append(
            "GPU bootstrap: no advertised GPU yet; verify DGX OS driver/hardware locally."
        )
    if "certificates" in deployment.acting and isinstance(p.tls, ACMETLS):
        warnings.append(
            f"{p.tls.mode} DNS credentials are supplied, but cloud permissions and authoritative "
            "DNS propagation are verified only by actual DNS-01 issuance."
        )
        if p.tls.environment == "staging":
            warnings.append(
                "ACME staging certificates are not publicly trusted; use only for testing."
            )
    elif "certificates" in deployment.acting:
        warnings.append(
            "Self-signed: managed clients receive trust; browsers, external clients and "
            "model-pulling "
            "nodes require manual certificate trust. No host trust stores are modified."
        )
    if (
        "ai-gateway" in set(deployment.components) | deployment.installed
        and "monitoring" not in set(deployment.components) | deployment.installed
    ):
        warnings.append("Tempo is absent; AI Gateway traces cannot be delivered.")
    # Compare new desired requests against capacity without double-counting
    # existing owned pods. Their owner labels are found on the top-level owner.
    report("Reading cluster-wide pod inventory for aggregate capacity...")
    pod_output = runner.kubectl(
        "get", "pods", "--all-namespaces", "-o", f"jsonpath={_POD_PREFLIGHT_TEMPLATE}"
    )
    pods = _parse_pod_inventory(pod_output)
    available = {
        key: sum((quantity(n["status"]["allocatable"].get(key, 0)) for n in ready), Decimal(0))
        for key in ("cpu", "memory")
    }
    for pod in pods:
        if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        labels = pod.get("metadata", {}).get("labels", {})
        if (
            labels.get(LABEL) == runner.installation
            and labels.get(COMPONENT_LABEL) in {component_label(name) for name in deployment.acting}
        ):
            continue
        for key, amount in requests(pod.get("spec", {})).items():
            if key in available:
                available[key] -= amount
    desired = dict.fromkeys(available, Decimal(0))
    for doc in [
        *[d for docs in deployment.resources.values() for d in docs],
        *[d for docs in deployment.templates.values() for d in docs],
    ]:
        spec = doc.get("spec", {})
        template = spec.get("template", {}).get("spec")
        if doc["kind"] in {"Prometheus", "Alertmanager", "ThanosRuler"}:
            template = {
                "containers": [
                    {"resources": spec.get("resources", {})},
                    *spec.get("containers", []),
                ]
            }
        if template:
            replicas = len(ready) if doc["kind"] == "DaemonSet" else spec.get("replicas", 1)
            for key, amount in requests(template).items():
                if key in desired:
                    desired[key] += amount * replicas
    for key in desired:
        if desired[key] > available[key]:
            raise StackError(
                f"Insufficient aggregate {key}: requests {desired[key]}, available {available[key]}"
            )
    report(
        "Capacity passed: "
        + "; ".join(f"{key} requests {desired[key]}, available {available[key]}" for key in desired)
        + "."
    )
    report("Reading cluster-wide ResourceQuotas...")
    quotas = runner.kubectl("get", "resourcequotas", "--all-namespaces", "-o", "name").splitlines()
    report(f"ResourceQuota check passed: found {len(quotas)}.")
    if quotas:
        warnings.append(
            "ResourceQuotas exist: verify namespace-specific headroom before installation."
        )
    return warnings
