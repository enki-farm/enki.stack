from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from uuid import uuid4

from enkictl.components import REGISTRY, phases, removal
from enkictl.components.base import COMPONENT_LABEL
from enkictl.config import ACMETLS, StackError, dump, private_write, read_yaml
from enkictl.postrender import KEEP, LABEL, component_label, mark
from enkictl.render import (
    GW_NS,
    TLS_NAME,
    Deployment,
    documents,
    resource,
)


def identity(doc: dict, default_namespace: str = "") -> dict:
    meta = doc["metadata"]
    return {
        "apiVersion": doc["apiVersion"],
        "kind": doc["kind"],
        "name": meta["name"],
        "namespace": meta.get("namespace", default_namespace),
    }


def api_kind(doc: dict) -> str:
    group = doc["apiVersion"].split("/")[0] if "/" in doc["apiVersion"] else ""
    return doc["kind"].lower() + ("." + group if group else "")


class Installation:
    def __init__(self, deployment: Deployment):
        self.deployment = deployment
        self.runner = deployment.runner
        self.config = deployment.config
        key = hashlib.sha256(str(self.config.path).encode()).hexdigest()[:16]
        self.path = self.config.path.parent / "inventory.yaml"
        self.legacy_path = deployment.root / ".generated" / "cli" / key / "inventory.json"
        source = self.path if self.path.exists() else self.legacy_path
        if self.path.exists():
            self.state = read_yaml(self.path)
        elif self.legacy_path.exists():
            self.state = json.loads(self.legacy_path.read_text())
        else:
            self.state = {
                "schema_version": 2,
                "id": uuid4().hex,
                "resources": [],
                "releases": [],
                "profile": str(self.config.path),
                "status": "new",
                "components": {},
                "addons": {},
                "desired": {},
                "credentials": {},
            }
        if not isinstance(self.state, dict):
            raise StackError(f"Invalid installation inventory at {source}")
        if self.state.get("schema_version") != 2:
            raise StackError(
                "Unsupported inventory schema; back up inventory.yaml and use the CLI version "
                "that created it."
            )
        desired = self.state.get("desired", {})
        if not isinstance(desired, dict) or any(
            key in desired
            and (
                not isinstance(desired[key], list)
                or any(not isinstance(item, str) for item in desired[key])
            )
            for key in ("components", "addons")
        ):
            raise StackError(f"Invalid desired selection in {source}")
        self.state.setdefault("addons", {})
        self.state["desired"] = desired
        self.runner.installation = self.state["id"]
        self.versions: set[str] | None = None
        self.component: str | None = None

    @property
    def exists(self) -> bool:
        return self.path.exists() or self.legacy_path.exists()

    def plan(self) -> None:
        d = self.deployment
        d.installed = {
            name
            for name, state in self.state["components"].items()
            if state["status"] == "installed"
        }
        d.acting = [
            name
            for name in d.components
            if name in d.requested
            or self.state["components"].get(name, {}).get("status") != "installed"
        ]
        d.releases = [release for release in d.releases if release.component in d.acting]

    def removal_targets(self, targets: list[str] | None = None) -> list[str]:
        active = [
            name
            for name, state in self.state["components"].items()
            if state["status"] not in {"uninstalled", "purged"}
        ]
        known = set(self.state["components"]) & set(REGISTRY)
        known.update(r["component"] for r in self.state["resources"] if r["component"] in REGISTRY)
        selected = targets or sorted(known)
        order = removal(selected, active)
        missing = set(order) - known
        if missing:
            raise StackError(
                "Components have no recorded installation: " + ", ".join(sorted(missing))
            )
        return order

    def save(self) -> None:
        private_write(self.path, dump(self.state))

    def discover(self) -> None:
        self.versions = {"v1"} | set(self.runner.kubectl("api-versions").splitlines())

    def live(self, doc: dict) -> dict | None:
        if self.versions is None:
            self.discover()
        if doc["apiVersion"] not in self.versions:
            return None
        meta = doc.get("metadata", doc)
        return self.runner.get(api_kind(doc), meta["name"], meta.get("namespace", ""))

    def bind(self, write: bool = False, configuration: bool = False) -> None:
        cluster_uid = self.runner.json("get", "namespace", "kube-system")["metadata"]["uid"]
        previous = self.state.get("cluster_uid")
        if previous and previous != cluster_uid:
            raise StackError("Inventory belongs to a different cluster; refusing operation")
        if configuration and (
            self.state.get("platform", self.config.profile.platform) != self.config.profile.platform
        ):
            raise StackError("Changing installation platform requires uninstall/purge first")
        profile = self.config.profile
        gpu_state = self.state["components"].get("gpu-operator", {})
        if (
            configuration
            and self.deployment.full
            and gpu_state.get("status") not in {None, "uninstalled", "purged"}
            and profile.gpu != "operator"
        ):
            raise StackError(
                "Changing GPU management requires uninstalling gpu-operator first; "
                "in-place topology migration is not supported"
            )
        topologies = {}
        for name in self.deployment.components:
            topology = (
                {"gpu": profile.gpu}
                if name == "gpu-operator"
                else {
                    "domain": profile.domain,
                    "tls_mode": getattr(profile.tls, "mode", None),
                    "acme_environment": getattr(profile.tls, "environment", None),
                }
            )
            previous_component = self.state["components"].get(name, {})
            if (
                configuration
                and previous_component.get("topology") not in (None, topology)
                and previous_component.get("status") not in {"uninstalled", "purged"}
            ):
                raise StackError(
                    f"Changing {name} topology requires uninstall first; "
                    "in-place topology migration is not supported"
                )
            topologies[name] = topology
        marker = self.runner.get("configmap", "enki-" + self.state["id"], "kube-system")
        if previous and not marker and self.state["status"] != "new":
            raise StackError(
                "Cluster installation marker is missing; refusing unverified ownership"
            )
        if marker and marker.get("data", {}).get("installation") != self.state["id"]:
            raise StackError("Cluster installation marker does not match local inventory")
        if write:
            self.state["cluster_uid"] = cluster_uid
            self.state["platform"] = self.config.profile.platform
            for name in self.deployment.acting:
                self.state["components"].setdefault(name, {"status": "new"})["topology"] = (
                    topologies[name]
                )
            self.save()
            self.apply(
                resource(
                    "ConfigMap",
                    "enki-" + self.state["id"],
                    "kube-system",
                    data={"installation": self.state["id"]},
                ),
                "marker",
            )

    def check_owned(self, doc: dict, live: dict) -> None:
        if live["metadata"].get("labels", {}).get(LABEL) != self.state["id"]:
            raise StackError(f"Foreign resource conflict: {doc['kind']}/{doc['name']}")
        record = next(
            (
                r
                for r in self.state["resources"]
                if all(r.get(k) == doc.get(k) for k in ("apiVersion", "kind", "name", "namespace"))
            ),
            None,
        )
        if record and record.get("uid") and record["uid"] != live["metadata"]["uid"]:
            raise StackError(f"Resource was replaced: {doc['kind']}/{doc['name']}")

    def conflicts(self) -> None:
        self.bind(configuration=True)
        namespaces = {
            doc["metadata"]["name"] for doc in self.runner.json("get", "namespaces")["items"]
        }
        resources: dict[tuple[str, str], dict[str, dict]] = {}
        releases: dict[str, list[dict]] = {}

        def lookup(doc: dict) -> dict | None:
            meta = doc.get("metadata", doc)
            namespace = meta.get("namespace", "")
            if namespace and namespace not in namespaces:
                return None
            if self.versions is None:
                self.discover()
            if doc["apiVersion"] not in self.versions:
                return None
            key = (api_kind(doc), namespace)
            if key not in resources:
                args = ["get", key[0]]
                if namespace:
                    args += ["-n", namespace]
                resources[key] = {
                    item["metadata"]["name"]: item for item in self.runner.json(*args)["items"]
                }
            return resources[key].get(meta["name"])

        for docs in self.deployment.resources.values():
            for doc in docs:
                key = identity(doc)
                live = lookup(doc)
                if not live:
                    continue
                if doc["kind"] == "CustomResourceDefinition":
                    if live["metadata"].get("labels", {}).get(LABEL) != self.state["id"]:
                        self.shared_crd(doc, live)
                        continue
                self.check_owned(key, live)
        for release in self.deployment.releases:
            if release.namespace not in releases:
                releases[release.namespace] = (
                    json.loads(
                        self.runner.helm(
                            "list", "--namespace", release.namespace, "--all", "--output", "json"
                        )
                    )
                    if release.namespace in namespaces
                    else []
                )
            if any(item["name"] == release.name for item in releases[release.namespace]):
                if not any(
                    r["name"] == release.name and r["namespace"] == release.namespace
                    for r in self.state["releases"]
                ):
                    raise StackError(f"Foreign Helm release: {release.namespace}/{release.name}")
            for doc in self.deployment.templates[release.name]:
                if not doc.get("metadata", {}).get("name") or doc["kind"] == "Namespace":
                    continue
                if doc.get("metadata", {}).get("annotations", {}).get("helm.sh/hook"):
                    continue
                # Helm renders cluster-scoped objects without a namespace.
                key = identity(doc, self.default_namespace(doc, release.namespace))
                live = lookup(key)
                if live:
                    self.check_owned(key, live)
        for namespace, name in self.generated_secret_names() + self.credential_names():
            live = lookup(resource("Secret", name, namespace))
            if live:
                self.check_owned(identity(resource("Secret", name, namespace)), live)
        for key, owner in self.required_credentials().items():
            if (
                key in self.state["credentials"] or owner not in self.deployment.acting
            ) and not self.config.secrets.generated.get(key):
                raise StackError(f"Restore missing generated credential: {key}")
        if all(self.config.secrets.generated.get(key) for key in self.required_credentials()):
            for namespace, name, values in self.credential_values():
                self.check_credentials(namespace, name, values, lookup=lookup)
        for name in set(self.deployment.components) - set(self.deployment.acting):
            self.verify_dependency(name, lookup=lookup)

    def verify_dependency(
        self, name: str, *, lookup: Callable[[dict], dict | None] | None = None
    ) -> None:
        records = [
            record
            for record in self.state["resources"]
            if record["component"] == name
            and record["status"] == "applied"
            and record["phase"] != "generated"
        ]
        if not records:
            raise StackError(f"Dependency {name} has no active recorded resources; reinstall it")
        live_resources = []
        for record in records:
            live = (lookup or self.live)(record)
            if not live:
                raise StackError(f"Dependency {name} is missing resources; reinstall it")
            self.check_owned(record, live)
            live_resources.append(live)
        self.wait_resources(live_resources)

    @staticmethod
    def shared_crd(desired: dict, live: dict) -> None:
        actual = live["spec"]
        expected = desired["spec"]
        if actual.get("group") != expected.get("group") or actual.get("scope") != expected.get(
            "scope"
        ):
            raise StackError(f"Incompatible shared CRD: {desired['metadata']['name']}")
        schemas = {v["name"]: v.get("schema") for v in actual["versions"]}
        if any(schemas.get(v["name"]) != v.get("schema") for v in expected["versions"]):
            raise StackError(
                f"Shared CRD schema differs: {desired['metadata']['name']}; no takeover"
            )

    def generated_secret_names(self, include_charts: bool = True) -> list[tuple[str, str]]:
        result = [(GW_NS, TLS_NAME)] if "certificates" in self.deployment.acting else []
        tls = self.config.profile.tls
        if "certificates" in self.deployment.acting and isinstance(tls, ACMETLS):
            result += [
                ("cert-manager", f"enki-acme-{tls.environment}"),
                ("cert-manager", "enki-dns01"),
            ]
        if include_charts:
            for release in self.deployment.releases:
                for doc in self.deployment.templates.get(release.name, []):
                    if doc["kind"] == "Certificate":
                        result.append(
                            (
                                doc["metadata"].get("namespace", release.namespace),
                                doc["spec"]["secretName"],
                            )
                        )
        return sorted(set(result))

    def credential_names(self) -> list[tuple[str, str]]:
        return [
            item
            for name in self.deployment.acting
            if name in REGISTRY
            for item in REGISTRY[name].secret_names
        ]

    def required_credentials(self) -> dict[str, str]:
        owners = {
            key: name for name, spec in REGISTRY.items() for key in spec.generated_credentials
        }
        return {
            key: owners[key]
            for name in self.deployment.acting
            if name in REGISTRY
            for key in REGISTRY[name].required_credentials
        }

    def default_namespace(self, doc: dict, namespace: str) -> str:
        cluster = {
            "CustomResourceDefinition",
            "ClusterRole",
            "ClusterRoleBinding",
            "Namespace",
            "ClusterPolicy",
            "GatewayClass",
            "ClusterIssuer",
            "Bundle",
            "MutatingWebhookConfiguration",
            "ValidatingWebhookConfiguration",
            "APIService",
            "PriorityClass",
            "RuntimeClass",
            "CSIDriver",
            "ClusterStorageContainer",
            "ClusterServingRuntime",
            "StorageClass",
            "PersistentVolume",
            "VolumeAttachment",
            "Node",
        }
        for docs in [*self.deployment.resources.values(), *self.deployment.templates.values()]:
            for definition in docs:
                if (
                    definition["kind"] == "CustomResourceDefinition"
                    and definition["spec"].get("scope") == "Cluster"
                ):
                    cluster.add(definition["spec"]["names"]["kind"])
        cluster.update(
            record["kind"] for record in self.state["resources"] if not record["namespace"]
        )
        return "" if doc["kind"] in cluster else namespace

    def intend(
        self, key: dict, phase: str, release: str | None = None, component: str | None = None
    ) -> dict:
        owner = component or (
            "shared"
            if phase in {"marker", "namespaces"}
            else self.component or self.deployment.phase_components.get(phase)
        )
        if owner is None:
            raise StackError(f"Cannot establish component ownership for {phase}")
        record = next(
            (
                r
                for r in self.state["resources"]
                if all(r.get(k) == key.get(k) for k in ("apiVersion", "kind", "name", "namespace"))
            ),
            None,
        )
        if record is None:
            record = {
                **key,
                "phase": phase,
                "status": "pending",
                "release": release,
                "component": owner,
            }
            self.state["resources"].append(record)
        elif record["component"] != owner:
            raise StackError(f"Conflicting component ownership: {key['kind']}/{key['name']}")
        if record["status"] == "deleted":
            record.pop("uid", None)
            record["status"] = "pending"
        self.save()
        return record

    def applied(self, record: dict, live: dict) -> None:
        record.update(uid=live["metadata"]["uid"], status="applied")
        self.save()

    def apply(self, doc: dict, phase: str) -> None:
        owner = (
            "shared"
            if phase in {"marker", "namespaces"}
            else self.component or self.deployment.phase_components.get(phase)
        )
        if owner is None:
            raise StackError(f"Cannot establish component ownership for {phase}")
        doc = mark(copy.deepcopy(doc), self.state["id"], owner)
        live = self.live(doc)
        key = identity(doc)
        if live:
            if doc["kind"] == "CustomResourceDefinition":
                if live["metadata"].get("labels", {}).get(LABEL) != self.state["id"]:
                    self.shared_crd(doc, live)
                    return
            self.check_owned(key, live)
        record = self.intend(key, phase)
        if doc["kind"] == "CustomResourceDefinition" and doc.get("metadata", {}).get(
            "annotations", {}
        ).get("meta.helm.sh/release-name"):
            record["release"] = doc["metadata"]["annotations"]["meta.helm.sh/release-name"]
        self.runner.kubectl(
            "apply", "--server-side", "--field-manager=enki-stack", "-f", "-", data=dump(doc)
        )
        if doc["kind"] == "CustomResourceDefinition":
            self.wait_crd(key["name"])
            self.versions = None
        live = self.live(doc)
        if not live:
            raise StackError(f"Applied resource disappeared: {key['kind']}/{key['name']}")
        self.applied(record, live)

    def wait_crd(self, name: str, timeout: int = 120) -> None:
        deadline = time.monotonic() + timeout
        while True:
            live = self.runner.get("customresourcedefinition", name)
            if live is None:
                raise StackError(f"CRD {name} disappeared while waiting for Established=True")
            # kubectl wait rejects null conditions before the API server establishes a CRD.
            conditions = (live.get("status") or {}).get("conditions") or []
            if any(
                condition.get("type") == "Established" and condition.get("status") == "True"
                for condition in conditions
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StackError(
                    f"CRD {name} did not reach Established=True after {timeout}s. "
                    f"Last conditions: {json.dumps(conditions)}"
                )
            time.sleep(min(1, remaining))

    def check_credentials(
        self,
        namespace: str,
        name: str,
        values: dict,
        *,
        lookup: Callable[[dict], dict | None] | None = None,
    ) -> None:
        existing = (
            lookup(resource("Secret", name, namespace))
            if lookup is not None
            else self.runner.get("secret", name, namespace)
        )
        if existing:
            locked = {
                "dex-secrets": {
                    "GRAFANA_CLIENT_SECRET",
                    "HUB_CLIENT_SECRET",
                    "LIBRECHAT_CLIENT_SECRET",
                },
                "oidc-credentials": {"client-secret"},
                "hub-oidc": {"client-secret"},
                "grafana-admin": {"admin-user", "admin-password"},
                "model-registry-db-secrets": {"MYSQL_USER_NAME", "MYSQL_ROOT_PASSWORD"},
                "model-catalog-postgres": {"POSTGRES_USER", "POSTGRES_PASSWORD"},
                "zot-auth": {"hub-password", "kserve-password"},
            }.get(name, set())
            for key in locked:
                if (
                    name == "dex-secrets"
                    and key == "LIBRECHAT_CLIENT_SECRET"
                    and key not in existing.get("data", {})
                ):
                    continue
                stored = base64.b64decode(existing.get("data", {}).get(key, "")).decode()
                if stored != values.get(key):
                    raise StackError(
                        f"Credential mismatch {namespace}/{name}:{key}; restore credentials, "
                        "automatic rotation is not supported"
                    )

    def secret(self, namespace: str, name: str, values: dict, kind: str = "Opaque") -> None:
        if self.component is None:
            raise StackError("Secret creation requires explicit component ownership")
        self.check_credentials(namespace, name, values)
        self.apply(
            resource(
                "Secret",
                name,
                namespace,
                type=kind,
                data={
                    key: base64.b64encode(value.encode()).decode() for key, value in values.items()
                },
            ),
            REGISTRY[self.component].phases[0],
        )

    def credential_values(self) -> list[tuple[str, str, dict]]:
        result = []
        for name in self.deployment.acting:
            build = REGISTRY[name].credentials
            if build:
                result.extend(
                    (secret.namespace, secret.name, secret.values)
                    for secret in build(self.deployment)
                )
        return result

    def credentials(self, component: str | None = None) -> None:
        generated = self.config.secrets.generated
        # Older auth inventories predate the LibreChat client. Never rotate a registered key.
        if (
            "auth" in self.deployment.acting
            and "librechat" not in self.state["credentials"]
            and not generated.get("librechat")
        ):
            existing = self.runner.get("secret", "dex-secrets", "dex")
            stored = existing.get("data", {}).get("LIBRECHAT_CLIENT_SECRET") if existing else None
            if stored:
                try:
                    generated["librechat"] = base64.b64decode(stored, validate=True).decode()
                except (ValueError, UnicodeDecodeError):
                    raise StackError(
                        "Dex LibreChat client credential has invalid encoding"
                    ) from None
            else:
                config = self.runner.get("configmap", "dex-config", "dex")
                if config:
                    clients = documents(config["data"]["config.yaml"])[0].get("staticClients", [])
                    if any(client.get("id") == "librechat" for client in clients):
                        raise StackError(
                            "Restore missing Dex LibreChat client credential; refusing rotation"
                        )
                generated["librechat"] = secrets.token_hex(32)
        for name, owner in self.required_credentials().items():
            if not generated.get(name):
                if (
                    owner not in self.deployment.acting
                    or name in self.state["credentials"]
                    or any(
                        r["component"] == owner
                        and r["kind"] == "Secret"
                        and r["status"] == "applied"
                        for r in self.state["resources"]
                    )
                ):
                    raise StackError(f"Restore missing generated credential: {name}")
                generated[name] = secrets.token_hex(32)
            self.state["credentials"][name] = {"component": owner}
        self.config.save_secrets()
        self.save()
        for owner in self.deployment.acting:
            if component and owner != component:
                continue
            build = REGISTRY[owner].credentials
            if build is None:
                continue
            self.component = owner
            for secret in build(self.deployment):
                self.secret(secret.namespace, secret.name, secret.values, secret.kind)
        self.component = component
        self.save()

    def helm_install(self, release) -> None:
        self.runner.component = release.component
        self.runner.helm_exclude_crds = release.exclude_crds
        renderer = self.runner.post_renderer()
        existing = next(
            (
                r
                for r in self.state["releases"]
                if r["name"] == release.name and r["namespace"] == release.namespace
            ),
            None,
        )
        if existing is None:
            existing = {
                "name": release.name,
                "namespace": release.namespace,
                "phase": release.phase,
                "component": release.component,
                "status": "pending",
            }
            self.state["releases"].append(existing)
        self.save()
        docs = self.deployment.templates[release.name]
        # Helm's crds/ directory is not post-rendered or upgraded. Manage CRDs
        # ourselves; chart-templated CRDs get retention through the post-renderer.
        for doc in docs:
            if doc["kind"] == "CustomResourceDefinition":
                owned = copy.deepcopy(doc)
                owned["metadata"].setdefault("annotations", {}).update(
                    {
                        "meta.helm.sh/release-name": release.name,
                        "meta.helm.sh/release-namespace": release.namespace,
                    }
                )
                owned["metadata"].setdefault("labels", {})["app.kubernetes.io/managed-by"] = "Helm"
                self.apply(owned, release.phase)
        for doc in docs:
            if doc.get("metadata", {}).get("name") and not doc.get("metadata", {}).get(
                "annotations", {}
            ).get("helm.sh/hook"):
                key = identity(doc, self.default_namespace(doc, release.namespace))
                self.intend(key, release.phase, release.name)
        self.runner.helm(
            "upgrade",
            "--install",
            release.name,
            *release.args(self.deployment.root, self.config.profile.platform),
            "--wait",
            "--timeout=10m",
            "--skip-crds",
            "--post-renderer",
            renderer,
            "-f",
            "-",
            data=dump(self.deployment.overrides.get(release.name, {})),
        )
        # Capture actual manifest names (including generated chart content).
        manifest = documents(
            self.runner.helm("get", "manifest", release.name, "--namespace", release.namespace)
        )
        for doc in manifest:
            if not doc.get("metadata", {}).get("name"):
                continue
            key = identity(doc, self.default_namespace(doc, release.namespace))
            live = self.live(key)
            if live:
                self.check_owned(key, live)
                self.applied(self.intend(key, release.phase, release.name), live)
            if doc["kind"] == "Certificate":
                self.register_generated_secret(key["namespace"], doc["spec"]["secretName"])
        existing["status"] = "applied"
        self.runner.component = None
        self.runner.helm_exclude_crds = []
        self.save()

    def register_generated_secret(self, namespace: str, name: str) -> None:
        key = identity(resource("Secret", name, namespace))
        previous = next(
            (r for r in self.state["resources"] if all(r.get(k) == v for k, v in key.items())),
            None,
        )
        owner = previous["component"] if previous else self.component
        if not owner:
            raise StackError(f"Cannot establish generated Secret ownership: {namespace}/{name}")
        record = self.intend(key, "certificate-generated", component=owner)
        live = self.runner.get("secret", name, namespace)
        if not live:
            raise StackError(f"Expected cert-manager generated Secret missing: {namespace}/{name}")
        # Preflight proved absence/previous ownership before the issuer was applied.
        other = live["metadata"].get("labels", {}).get(LABEL)
        if other and other != self.state["id"]:
            raise StackError(f"Generated Secret has foreign owner: {namespace}/{name}")
        self.runner.kubectl(
            "label", "secret", name, "-n", namespace, f"{LABEL}={self.state['id']}", "--overwrite"
        )
        self.applied(record, self.runner.get("secret", name, namespace))

    def install(self) -> None:
        self.bind(write=True, configuration=True)
        self.state["status"] = "installing"
        self.save()
        namespaces = sorted(
            {
                namespace
                for name in self.deployment.acting
                for namespace in REGISTRY[name].namespaces
            }
        )
        self.component = None
        for namespace in namespaces:
            if not self.runner.get("namespace", namespace):
                self.apply(resource("Namespace", namespace), "namespaces")
            if getattr(self.config.profile.tls, "mode", None) == "self-signed" and namespace in {
                "observability",
                "kubeflow",
            }:
                self.runner.kubectl(
                    "label", "namespace", namespace, "enki.stack/trust=true", "--overwrite"
                )
        # Intend generated names before cert-manager can create them.
        for namespace, name in self.generated_secret_names(include_charts=False):
            if name != "enki-dns01":
                self.intend(
                    identity(resource("Secret", name, namespace)),
                    "certificate-generated",
                    component="certificates",
                )
        tls = self.config.profile.tls
        if "certificates" in self.deployment.acting and isinstance(tls, ACMETLS):
            self.component = "certificates"
            self.secret(
                "cert-manager",
                "enki-dns01",
                {
                    "client-secret"
                    if tls.mode == "azure"
                    else "api-token": self.config.secrets.azure_client_secret
                    if tls.mode == "azure"
                    else self.config.secrets.cloudflare_api_token
                },
            )
        for phase in phases(self.deployment.acting):
            self.component = self.deployment.phase_components[phase]
            if self.component not in self.deployment.acting:
                continue
            self.state["components"][self.component]["status"] = "installing"
            self.state["phase"] = phase
            self.save()
            if phase in {"auth", "monitoring", "hub"}:
                self.credentials(self.component)
            # Prerequisite CRDs before Helm; trust-manager creates Bundle CRDs itself.
            if phase == "gateway-controller":
                for doc in self.deployment.resources[phase]:
                    self.apply(doc, phase)
            for release in self.deployment.releases:
                if release.phase == phase:
                    self.helm_install(release)
                    self.versions = None
            if phase != "gateway-controller":
                docs = self.deployment.resources[phase]
                docs = sorted(docs, key=lambda d: d["kind"] != "CustomResourceDefinition")
                for doc in docs:
                    self.apply(doc, phase)
            self.wait_phase(phase)
            self.capture_generated()
            if phase == REGISTRY[self.component].phases[-1]:
                self.state["components"][self.component]["status"] = "installed"
                self.save()
        for name in self.deployment.acting:
            self.state["components"][name]["status"] = "installed"
        self.component = None
        self.state["status"] = "installed"
        self.save()

    def capture_generated(self) -> None:
        """Track persistent children using owner-UID chains, not name guesses."""
        if self.versions is None:
            self.discover()
        types = [
            ("apps/v1", "deployments"),
            ("apps/v1", "replicasets"),
            ("apps/v1", "statefulsets"),
            ("batch/v1", "jobs"),
            ("v1", "pods"),
            ("v1", "persistentvolumeclaims"),
            ("v1", "secrets"),
            ("v1", "configmaps"),
            ("cert-manager.io/v1", "certificaterequests.cert-manager.io"),
            ("acme.cert-manager.io/v1", "orders.acme.cert-manager.io"),
            ("acme.cert-manager.io/v1", "challenges.acme.cert-manager.io"),
        ]
        items = []
        for version, kind in types:
            if version in self.versions:
                items.extend(self.runner.json("get", kind, "--all-namespaces")["items"])
        owners = {r["uid"]: r["component"] for r in self.state["resources"] if r.get("uid")}
        changed = True
        owned = [
            doc
            for doc in items
            if doc["metadata"]["uid"] not in owners
            and doc["metadata"].get("labels", {}).get(LABEL) == self.state["id"]
        ]
        component_owners = {
            component_label(name): name
            for name in [*REGISTRY, *(f"addon:{name}" for name in self.state["addons"])]
        }
        for doc in owned:
            owner = component_owners.get(doc["metadata"].get("labels", {}).get(COMPONENT_LABEL))
            if owner is None:
                raise StackError("Cannot establish component ownership of generated resource")
            owners[doc["metadata"]["uid"]] = owner
        while changed:
            changed = False
            for doc in items:
                meta = doc["metadata"]
                if meta["uid"] in owners:
                    continue
                parent_components = {
                    owners[ref["uid"]]
                    for ref in meta.get("ownerReferences", [])
                    if ref["uid"] in owners
                }
                if len(parent_components) > 1:
                    raise StackError("Ambiguous generated resource component ownership")
                if parent_components:
                    owners[meta["uid"]] = parent_components.pop()
                    owned.append(doc)
                    changed = True
        retained = {
            "Secret",
            "PersistentVolumeClaim",
            "ConfigMap",
            "CertificateRequest",
            "Order",
            "Challenge",
        }
        for doc in owned:
            if doc["kind"] not in retained:
                continue
            component = owners[doc["metadata"]["uid"]]
            if any(
                owners[ref["uid"]] != component
                for ref in doc["metadata"].get("ownerReferences", [])
                if ref["uid"] in owners
            ):
                raise StackError("Conflicting generated resource component ownership")
            active_addons = {
                f"addon:{name}"
                for name, state in self.state["addons"].items()
                if state.get("status") in {"installing", "installed", "uninstalling"}
            }
            if component not in self.deployment.acting and component not in active_addons:
                continue
            key = identity(doc)
            other = doc["metadata"].get("labels", {}).get(LABEL)
            if other and other != self.state["id"]:
                raise StackError(
                    f"Generated resource has foreign owner: {key['kind']}/{key['name']}"
                )
            self.runner.kubectl(
                "label",
                api_kind(doc),
                key["name"],
                "-n",
                key["namespace"],
                f"{LABEL}={self.state['id']}",
                "--overwrite",
            )
            self.applied(self.intend(key, "generated", component=component), doc)

    def wait_phase(self, phase: str) -> None:
        ready = REGISTRY[self.deployment.phase_components[phase]].ready
        if ready:
            ready(self, phase)
        self.wait_resources(self.deployment.resources.get(phase, []))

    def wait_resources(self, docs: list[dict]) -> None:
        for doc in docs:
            if doc["kind"] in {"Deployment", "StatefulSet", "DaemonSet"}:
                meta = doc["metadata"]
                self.runner.kubectl(
                    "rollout",
                    "status",
                    f"{doc['kind'].lower()}/{meta['name']}",
                    "-n",
                    meta.get("namespace", "default"),
                    "--timeout=600s",
                )
            elif doc["kind"] == "HTTPRoute":
                for condition in ("Accepted", "ResolvedRefs"):
                    self.runner.kubectl(
                        "wait",
                        f"httproute/{doc['metadata']['name']}",
                        "-n",
                        doc["metadata"]["namespace"],
                        "--timeout=180s",
                        "--for=jsonpath={.status.parents[0].conditions"
                        f'[?(@.type=="{condition}")].status' + "}=True",
                    )
            elif doc["kind"] == "SecurityPolicy":
                self.runner.kubectl(
                    "wait",
                    f"securitypolicy/{doc['metadata']['name']}",
                    "-n",
                    doc["metadata"]["namespace"],
                    "--timeout=180s",
                    "--for=jsonpath={.status.ancestors[0].conditions"
                    '[?(@.type=="Accepted")].status}=True',
                )
            elif doc["kind"] in {"Certificate", "ClusterIssuer", "Gateway", "Bundle"}:
                condition = {
                    "Certificate": "Ready",
                    "ClusterIssuer": "Ready",
                    "Gateway": "Programmed",
                    "Bundle": "Synced",
                }[doc["kind"]]
                meta = doc["metadata"]
                namespace = ["-n", meta["namespace"]] if meta.get("namespace") else []
                self.runner.kubectl(
                    "wait",
                    f"{doc['kind'].lower()}/{meta['name']}",
                    *namespace,
                    f"--for=condition={condition}",
                    "--timeout=600s",
                )

    def delete(self, record: dict) -> None:
        live = self.live(record)
        if live is None:
            record["status"] = "deleted"
            self.save()
            return
        self.check_owned(record, live)
        # API DeleteOptions UID preconditions prevent deleting a replacement
        # between the ownership check and the delete request.
        group = record["apiVersion"]
        discovery = self.runner.json(
            "get", "--raw=" + ("/api/v1" if group == "v1" else "/apis/" + group)
        )
        plural = next(
            r["name"]
            for r in discovery["resources"]
            if r["kind"] == record["kind"] and "/" not in r["name"]
        )
        prefix = "/api/v1" if group == "v1" else "/apis/" + group
        if record.get("namespace"):
            prefix += "/namespaces/" + record["namespace"]
        self.runner.kubectl(
            "delete",
            "--raw=" + f"{prefix}/{plural}/{record['name']}",
            "-f",
            "-",
            data=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": live["metadata"]["uid"]},
                }
            ),
        )
        wait = ["wait", "--for=delete", f"{api_kind(record)}/{record['name']}", "--timeout=180s"]
        if record.get("namespace"):
            wait += ["-n", record["namespace"]]
        self.runner.kubectl(*wait)
        record["status"] = "deleted"
        self.save()

    def uninstall(self, purge: bool, targets: list[str] | None = None) -> list[str]:
        if not self.exists:
            raise StackError("No installation inventory; refusing untracked deletion")
        self.bind()
        selected = self.removal_targets(targets)
        self.deployment.acting = selected
        warnings = []
        self.capture_generated()
        records = [r for r in self.state["resources"] if r["component"] in selected]
        releases_to_remove = [r for r in self.state["releases"] if r["component"] in selected]
        for release in releases_to_remove:
            releases = json.loads(
                self.runner.helm(
                    "list", "--namespace", release["namespace"], "--all", "--output", "json"
                )
            )
            if not any(r["name"] == release["name"] for r in releases):
                continue
            manifest = documents(
                self.runner.helm("get", "manifest", release["name"], "-n", release["namespace"])
            )
            for doc in manifest:
                if doc["metadata"].get("labels", {}).get(LABEL) != self.state["id"]:
                    raise StackError(f"Helm release was replaced: {release['name']}")
                key = identity(doc, self.default_namespace(doc, release["namespace"]))
                live = self.live(key)
                if live:
                    self.check_owned(key, live)
        # Verify every live object before beginning deletion.
        for record in records:
            live = self.live(record)
            if live:
                if (
                    record["phase"] == "certificate-generated"
                    and not record.get("uid")
                    and not live["metadata"].get("labels", {}).get(LABEL)
                ):
                    record["status"] = "unverified"
                    warnings.append(
                        f"Retaining unverified generated Secret "
                        f"{record['namespace']}/{record['name']}; ownership was not established"
                    )
                    continue
                self.check_owned(record, live)
        self.state["status"] = "uninstalling"
        for name in selected:
            self.state["components"].setdefault(name, {})["status"] = "removing"
        self.save()
        # Detach cert-manager Secret owner references before removing Certificate
        # objects, regardless of controller enable-certificate-owner-ref flags.
        for record in records:
            if (
                record["kind"] in {"Secret", "PersistentVolumeClaim"}
                and record["status"] != "unverified"
            ):
                live = self.live(record)
                if live and live["metadata"].get("ownerReferences"):
                    self.runner.kubectl(
                        "patch",
                        api_kind(record),
                        record["name"],
                        "-n",
                        record["namespace"],
                        "--type=merge",
                        "-p",
                        '{"metadata":{"ownerReferences":[]}}',
                    )
        # Reverse dependency phases, keeping controllers alive until their
        # resources and webhooks have been removed.
        for phase in (phase for name in selected for phase in reversed(REGISTRY[name].phases)):
            for record in reversed(records):
                if (
                    record["phase"] == "generated"
                    and record["kind"] not in KEEP
                    and record["component"] == self.deployment.phase_components[phase]
                ):
                    self.delete(record)
            for record in reversed(records):
                if record["phase"] != phase or record.get("release") or record["kind"] in KEEP:
                    continue
                self.delete(record)
            for release in reversed(releases_to_remove):
                if release.get("phase") != phase:
                    continue
                releases = json.loads(
                    self.runner.helm(
                        "list", "--namespace", release["namespace"], "--all", "--output", "json"
                    )
                )
                if any(r["name"] == release["name"] for r in releases):
                    self.runner.helm(
                        "uninstall",
                        release["name"],
                        "-n",
                        release["namespace"],
                        "--wait",
                        "--timeout=10m",
                    )
                release["status"] = "deleted"
                self.save()
        if purge:
            for record in reversed(records):
                if (
                    record["kind"] in {"Secret", "PersistentVolumeClaim"}
                    and record["status"] != "unverified"
                ):
                    self.delete(record)
            for record in reversed(records):
                if record["kind"] != "CustomResourceDefinition":
                    continue
                live = self.live(record)
                if not live:
                    continue
                spec = live["spec"]
                try:
                    instances = self.runner.json(
                        "get", f"{spec['names']['plural']}.{spec['group']}", "--all-namespaces"
                    )["items"]
                except StackError:
                    warnings.append(f"Retaining CRD {record['name']}: cannot prove it is unused")
                    continue
                if instances:
                    warnings.append(f"Retaining CRD {record['name']}: instances still exist")
                    continue
                self.delete(record)
                self.versions = None
        for name in selected:
            self.state["components"][name]["status"] = "purged" if purge else "uninstalled"
        active = any(
            state["status"] not in {"uninstalled", "purged"}
            for state in self.state["components"].values()
        ) or any(
            state.get("status") not in {"uninstalled", "purged"}
            for state in self.state["addons"].values()
        )
        self.state["status"] = "installed" if active else ("purged" if purge else "uninstalled")
        self.save()
        return warnings

    def uninstall_addon(self, name: str, purge: bool = False) -> list[str]:
        from enkictl.addons import REGISTRY as ADDONS

        for dependent, spec in ADDONS.items():
            if name in getattr(spec, "addon_dependencies", ()) and self.state["addons"].get(
                dependent, {}
            ).get("status") not in {None, "uninstalled", "purged"}:
                raise StackError(
                    f"Cannot remove dependency of addon {dependent}; uninstall {dependent} first"
                )
        owner = f"addon:{name}"
        addon_state = self.state["addons"].get(name)
        if not self.exists or not addon_state:
            raise StackError(
                f"No recorded installation for addon {name}; refusing untracked deletion"
            )
        self.bind()
        if addon_state.get("status") in {"uninstalled", "purged"}:
            if not purge:
                raise StackError(f"Addon {name} is already uninstalled")
        addon_state["status"] = "uninstalling"
        self.component = owner
        self.save()
        try:
            self.capture_generated()
            records = [r for r in self.state["resources"] if r["component"] == owner]
            releases = [r for r in self.state["releases"] if r["component"] == owner]

            for release in releases:
                live_releases = json.loads(
                    self.runner.helm(
                        "list", "--namespace", release["namespace"], "--all", "--output", "json"
                    )
                )
                if not any(r["name"] == release["name"] for r in live_releases):
                    continue
                manifest = documents(
                    self.runner.helm(
                        "get", "manifest", release["name"], "--namespace", release["namespace"]
                    )
                )
                for doc in manifest:
                    if doc.get("metadata", {}).get("labels", {}).get(LABEL) != self.state["id"]:
                        raise StackError(f"Helm release was replaced: {release['name']}")
                    key = identity(doc, self.default_namespace(doc, release["namespace"]))
                    live = self.live(key)
                    if live:
                        self.check_owned(key, live)

            for record in records:
                live = self.live(record)
                if live:
                    self.check_owned(record, live)

            for record in records:
                if record["kind"] not in {"Secret", "PersistentVolumeClaim"}:
                    continue
                live = self.live(record)
                if live and live["metadata"].get("ownerReferences"):
                    self.runner.kubectl(
                        "patch",
                        api_kind(record),
                        record["name"],
                        "-n",
                        record["namespace"],
                        "--type=merge",
                        "-p",
                        '{"metadata":{"ownerReferences":[]}}',
                    )

            for record in reversed(records):
                if record.get("release") or record["kind"] in KEEP:
                    continue
                self.delete(record)

            for release in reversed(releases):
                live_releases = json.loads(
                    self.runner.helm(
                        "list", "--namespace", release["namespace"], "--all", "--output", "json"
                    )
                )
                if any(r["name"] == release["name"] for r in live_releases):
                    self.runner.helm(
                        "uninstall",
                        release["name"],
                        "-n",
                        release["namespace"],
                        "--wait",
                        "--timeout=10m",
                    )
                release["status"] = "deleted"
                self.save()

            if purge:
                for record in reversed(records):
                    if record["kind"] in {"Secret", "PersistentVolumeClaim"}:
                        self.delete(record)

            addon_state["status"] = "purged" if purge else "uninstalled"
            active = any(
                state.get("status") not in {"uninstalled", "purged"}
                for state in self.state["components"].values()
            ) or any(
                state.get("status") not in {"uninstalled", "purged"}
                for state in self.state["addons"].values()
            )
            self.state["status"] = "installed" if active else ("purged" if purge else "uninstalled")
            self.component = None
            self.save()
            return []
        except StackError:
            addon_state["status"] = "failed"
            self.component = None
            self.save()
            raise

    def addon_removal_preview(
        self, name: str, purge: bool = False
    ) -> list[tuple[str, str, str, str]]:
        owner = f"addon:{name}"
        addon_state = self.state["addons"].get(name)
        if not self.exists or not addon_state:
            raise StackError(
                f"No recorded installation for addon {name}; refusing untracked deletion"
            )
        if addon_state.get("status") in {"uninstalled", "purged"} and not purge:
            raise StackError(f"Addon {name} is already uninstalled")
        self.bind()
        records = [r for r in self.state["resources"] if r["component"] == owner]
        releases = [r for r in self.state["releases"] if r["component"] == owner]
        for release in releases:
            live_releases = json.loads(
                self.runner.helm(
                    "list", "--namespace", release["namespace"], "--all", "--output", "json"
                )
            )
            if not any(r["name"] == release["name"] for r in live_releases):
                continue
            manifest = documents(
                self.runner.helm(
                    "get", "manifest", release["name"], "--namespace", release["namespace"]
                )
            )
            for doc in manifest:
                if doc.get("metadata", {}).get("labels", {}).get(LABEL) != self.state["id"]:
                    raise StackError(f"Helm release was replaced: {release['name']}")
                key = identity(doc, self.default_namespace(doc, release["namespace"]))
                live = self.live(key)
                if live:
                    self.check_owned(key, live)

        result = []
        for record in records:
            live = self.live(record)
            if live:
                self.check_owned(record, live)
            retained = record["kind"] in KEEP and not purge
            action = "retain" if retained else "remove"
            if record["kind"] == "Namespace":
                action = "retain"
            result.append((action, record["kind"], record.get("namespace", ""), record["name"]))
        result.extend(
            ("uninstall Helm release", "HelmRelease", r["namespace"], r["name"]) for r in releases
        )
        return result
