from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from enkictl.components import REGISTRY, defaults, phases, resolve
from enkictl.config import ACMETLS, Configuration, KubernetesLoader, StackError, dump, read_yaml
from enkictl.tools import Runner

GW_NS = "envoy-ai-gateway-system"
TLS_NAME = "enki-platform-tls"
TRUST = "enki-platform-trust"
PHASES = phases(REGISTRY)


def documents(text: str) -> list[dict]:
    try:
        return [
            doc for doc in yaml.load_all(text, Loader=KubernetesLoader) if isinstance(doc, dict)
        ]
    except yaml.YAMLError:
        raise StackError("Invalid rendered YAML; source content withheld") from None


def resource(kind: str, name: str, namespace: str = "", **fields) -> dict:
    versions = {
        "ClusterIssuer": "cert-manager.io/v1",
        "Certificate": "cert-manager.io/v1",
        "Bundle": "trust.cert-manager.io/v1alpha1",
        "Backend": "gateway.envoyproxy.io/v1alpha1",
    }
    metadata = {"name": name}
    if namespace:
        metadata["namespace"] = namespace
    return {"apiVersion": versions.get(kind, "v1"), "kind": kind, "metadata": metadata, **fields}


@dataclass
class Release:
    name: str
    namespace: str
    chart: str
    version: str
    phase: str
    values: list[str]
    component: str
    repo: str | None = None
    exclude_crds: list[str] = field(default_factory=list)

    def args(self, root: Path, platform: str) -> list[str]:
        args = [self.chart, "--version", self.version, "--namespace", self.namespace]
        if self.repo:
            args += ["--repo", self.repo]
        for path in self.values:
            args += ["-f", str(root / path.format(platform=platform))]
        return args


class Deployment:
    gateway_namespace = GW_NS
    resource = staticmethod(resource)
    documents = staticmethod(documents)

    def __init__(
        self, config: Configuration, root: Path, runner: Runner, targets: list[str] | None = None
    ):
        self.config, self.root, self.runner = config, root, runner
        self.profile = config.profile
        self.full = targets is None
        self.installed: set[str] = set()
        self.requested = list(
            dict.fromkeys(targets if targets is not None else defaults(self.profile.gpu))
        )
        self.components = resolve(self.requested)
        self.acting = list(self.components)
        self.resources: dict[str, list[dict]] = {phase: [] for phase in PHASES}
        self.phase_components = {
            phase: name for name, spec in REGISTRY.items() for phase in spec.phases
        }
        self.releases = [
            Release(**item)
            for item in read_yaml(root / "helm/platform.yaml")["releases"]
            if item["component"] in self.components
            and not (
                item["name"] in {"trust-manager", "enki-trust-reloader"}
                and (self.profile.tls is None or self.profile.tls.mode != "self-signed")
            )
        ]
        self.overrides: dict[str, dict] = {}
        self.templates: dict[str, list[dict]] = {}

    @property
    def issuer_name(self) -> str:
        tls = self.profile.tls
        suffix = f"-{tls.environment}" if isinstance(tls, ACMETLS) else ""
        return f"enki-platform-{tls.mode}{suffix}"

    def load(self, relative: str, kustomize: bool = False) -> list[dict]:
        path = self.root / relative
        return documents(self.runner.render(path) if kustomize else path.read_text())

    def transform(self, value: object) -> object:
        if isinstance(value, dict):
            return {key: self.transform(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.transform(child) for child in value]
        if isinstance(value, str):
            if self.profile.domain:
                value = value.replace("zer0.garden", self.profile.domain)
            if self.profile.auth and self.profile.auth.hub_group:
                value = value.replace("enki-farm:core", self.profile.auth.hub_group)
            return value
        return value

    def template(self, relative: str, tokens: dict[str, str]) -> list[dict]:
        text = (self.root / relative).read_text()
        for key, value in tokens.items():
            # Templates quote scalars; encode safely before replacing inside quotes.
            text = text.replace(f"@{key}@", value.replace("\\", "\\\\").replace('"', '\\"'))
        docs = documents(text)
        if re.search(r"@[A-Z_]+@", dump(docs)):
            raise StackError(f"Unresolved template token in {relative}")
        return docs

    def tokens(self) -> dict[str, str]:
        p = self.profile
        return {
            "DEX_ISSUER": p.origin("auth"),
            "DEX_HOST": f"auth.{p.domain}",
            "GRAFANA_URL": p.origin("grafana"),
            "GRAFANA_HOST": f"grafana.{p.domain}",
            "GATEWAY_NAME": "envoy-ai-gateway-basic",
            "GATEWAY_NAMESPACE": GW_NS,
        }

    def compose(self) -> None:
        for name in self.acting:
            REGISTRY[name].compose(self)
        for phase in PHASES:
            self.resources[phase] = [
                self.transform(doc) for doc in self.resources[phase] if doc["kind"] != "Namespace"
            ]

    def render_charts(self, kube_version: str | None = None) -> None:
        renderer = self.runner.post_renderer()
        for release in self.releases:
            self.runner.component = release.component
            self.runner.helm_exclude_crds = release.exclude_crds
            override = self.overrides.get(release.name, {})
            rendered = self.runner.helm(
                "template",
                release.name,
                *release.args(self.root, self.profile.platform),
                "--include-crds",
                "--post-renderer",
                renderer,
                *(["--kube-version", kube_version] if kube_version else []),
                "-f",
                "-",
                data=dump(override),
            )
            self.templates[release.name] = documents(rendered)
        self.runner.component = None
        self.runner.helm_exclude_crds = []
        # Envoy extension CRDs are a prerequisite, separately applied without
        # taking ownership of Gateway API definitions.
        if "ai-gateway" not in self.acting:
            return
        gateway_release = next((release for release in self.releases if release.name == "eg"), None)
        if gateway_release is None:
            raise StackError("ai-gateway requires the eg release pin in helm/platform.yaml")
        self.resources["gateway-controller"] += documents(
            self.runner.helm(
                "template",
                "eg-crds",
                "oci://docker.io/envoyproxy/gateway-crds-helm",
                "--version",
                gateway_release.version,
                "--set",
                "crds.gatewayAPI.enabled=false",
                "--set",
                "crds.envoyGateway.enabled=true",
            )
        )
