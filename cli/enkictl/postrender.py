"""Helm post-renderer: attach ownership and protect persistent/shared objects."""

import os
import sys
from decimal import Decimal

import yaml

from enkictl.components.base import COMPONENT_LABEL
from enkictl.config import KubernetesLoader

LABEL = "enki.stack/installation"
KEEP = {"Secret", "PersistentVolumeClaim", "CustomResourceDefinition", "Namespace"}


def component_label(owner: str) -> str:
    return owner.replace("addon:", "addon-", 1) if owner.startswith("addon:") else owner


def _normalize_llm_cpu(value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _normalize_llm_cpu(item)
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in {"containers", "initContainers"} and isinstance(child, list):
                for container in child:
                    for resources in container.get("resources", {}).values():
                        if not isinstance(resources, dict) or "cpu" not in resources:
                            continue
                        cpu = str(resources["cpu"])
                        # The controller canonicalizes whole cores; SSA compares raw CRD values.
                        if cpu.endswith("m") and cpu[:-1].isdigit():
                            cores = Decimal(cpu[:-1]) / 1000
                            if cores == cores.to_integral_value():
                                cpu = str(int(cores))
                        resources["cpu"] = cpu
            _normalize_llm_cpu(child)


def mark(doc: dict, installation: str, component: str | None = None) -> dict:
    labels = {LABEL: installation}
    if component:
        labels[COMPONENT_LABEL] = component_label(component)
    metadata = doc.setdefault("metadata", {})
    metadata.setdefault("labels", {}).update(labels)
    if doc.get("kind") in KEEP:
        metadata.setdefault("annotations", {})["helm.sh/resource-policy"] = "keep"
    spec = doc.get("spec", {})
    if doc.get("kind") == "LLMInferenceServiceConfig":
        _normalize_llm_cpu(spec)
    if doc.get("kind") == "Certificate":
        spec.setdefault("secretTemplate", {}).setdefault("labels", {}).update(labels)
    # Custom resources may use spec.template for a PodSpec, which has no metadata.
    if (
        doc.get("kind")
        in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job"}
        and "template" in spec
    ):
        spec["template"].setdefault("metadata", {}).setdefault("labels", {}).update(labels)
    if doc.get("kind") == "StatefulSet":
        spec["persistentVolumeClaimRetentionPolicy"] = {
            "whenDeleted": "Retain",
            "whenScaled": "Retain",
        }
        for claim in spec.get("volumeClaimTemplates", []):
            claim.setdefault("metadata", {}).setdefault("labels", {}).update(labels)
    if doc.get("kind") in {"Prometheus", "Alertmanager", "ThanosRuler"}:
        spec.setdefault("podMetadata", {}).setdefault("labels", {}).update(labels)
        claim = spec.get("storage", {}).get("volumeClaimTemplate")
        if claim is not None:
            claim.setdefault("metadata", {}).setdefault("labels", {}).update(labels)
    return doc


def main() -> None:
    installation = os.environ.get("ENKI_INSTALLATION_ID")
    if not installation:
        print("Missing ENKI_INSTALLATION_ID for Helm post-renderer", file=sys.stderr)
        raise SystemExit(1)
    exclude_crds = set(os.environ.get("ENKI_HELM_EXCLUDE_CRDS", "").split(","))
    docs = [
        mark(doc, installation, os.environ.get("ENKI_COMPONENT"))
        for doc in yaml.load_all(sys.stdin, Loader=KubernetesLoader)
        if doc
        and not (
            doc.get("kind") == "CustomResourceDefinition"
            and doc.get("metadata", {}).get("name") in exclude_crds
        )
    ]
    yaml.safe_dump_all(docs, sys.stdout, sort_keys=False)
