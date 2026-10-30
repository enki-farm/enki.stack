import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from enkictl.addons.librechat import NAME, OWNER
from enkictl.config import StackError
from enkictl.lifecycle import Installation, identity
from enkictl.postrender import LABEL, mark
from enkictl.render import PHASES, Deployment, Release, resource

ROOT = Path(__file__).resolve().parents[2]


class Cluster:
    def __init__(self):
        self.installation = None
        self.objects = {}
        self.calls = []
        self.fail_apply = None
        self.put(resource("Namespace", "kube-system"))

    @staticmethod
    def key(doc):
        meta = doc.get("metadata", doc)
        return doc["kind"].lower(), meta.get("namespace", ""), meta["name"]

    def put(self, doc):
        doc = copy.deepcopy(doc)
        key = self.key(doc)
        previous = self.objects.get(key)
        doc["metadata"]["uid"] = previous["metadata"]["uid"] if previous else uuid4().hex
        if doc["kind"] == "CustomResourceDefinition":
            doc.setdefault("status", {"conditions": [{"type": "Established", "status": "True"}]})
        self.objects[key] = doc
        return doc

    def get(self, kind, name, namespace=""):
        return copy.deepcopy(self.objects.get((kind.split(".")[0].lower(), namespace, name)))

    def json(self, *args):
        self.calls.append(("read", args))
        if args[1].startswith("--raw="):
            return {
                "resources": [
                    {"kind": kind, "name": plural}
                    for kind, plural in (
                        ("ConfigMap", "configmaps"),
                        ("Secret", "secrets"),
                        ("PersistentVolumeClaim", "persistentvolumeclaims"),
                        ("CustomResourceDefinition", "customresourcedefinitions"),
                        ("Deployment", "deployments"),
                        ("Namespace", "namespaces"),
                    )
                ]
            }
        if args[1:3] == ("namespace", "kube-system"):
            return self.get("namespace", "kube-system")
        if args[1].startswith("widgets."):
            return {"items": self.foreign_instances}
        if args[0] == "get" and (len(args) == 2 or args[2:3] == ("-n",)):
            namespace = args[3] if "-n" in args else ""
            kind = args[1].split(".")[0].lower()
            if kind == "namespaces":
                kind = "namespace"
            return {
                "items": [
                    copy.deepcopy(doc)
                    for (doc_kind, doc_namespace, _), doc in self.objects.items()
                    if doc_kind == kind and doc_namespace == namespace
                ]
            }
        return {"items": []}

    def kubectl(self, *args, data=None):
        self.calls.append((args[0], args, data))
        if args[0] == "api-versions":
            return "apps/v1\ncert-manager.io/v1\napiextensions.k8s.io/v1\n"
        if args[0] == "apply":
            doc = yaml.safe_load(data)
            if doc["metadata"]["name"] == self.fail_apply:
                raise StackError("Injected apply failure")
            self.put(doc)
        elif args[0] == "delete":
            path = args[1].split("/")
            name, plural = path[-1], path[-2]
            kinds = {
                "configmaps": "configmap",
                "secrets": "secret",
                "persistentvolumeclaims": "persistentvolumeclaim",
                "customresourcedefinitions": "customresourcedefinition",
                "deployments": "deployment",
            }
            ns = path[path.index("namespaces") + 1] if "namespaces" in path else ""
            live = self.objects[(kinds[plural], ns, name)]
            assert json.loads(data)["preconditions"]["uid"] == live["metadata"]["uid"]
            del self.objects[(kinds[plural], ns, name)]
        return ""

    def helm(self, *args, data=None):
        self.calls.append(("helm", args, data))
        if args[0] == "list":
            return "[]"
        return ""


@pytest.fixture
def installation(config, tmp_path):
    cluster = Cluster()
    deployment = Deployment(config, ROOT, cluster)
    deployment.root = tmp_path
    deployment.releases = []
    return Installation(deployment)


def test_api_discovery_reads_only_served_version_names(installation):
    installation.discover()

    assert installation.versions == {
        "v1",
        "apps/v1",
        "cert-manager.io/v1",
        "apiextensions.k8s.io/v1",
    }
    assert any(call[0] == "api-versions" for call in installation.runner.calls)


@pytest.mark.parametrize(
    "status",
    [
        None,
        {},
        {"conditions": None},
        {"conditions": []},
        {"conditions": [{"type": "NamesAccepted", "status": "True"}]},
        {"conditions": [{"type": "Established", "status": "False"}]},
    ],
)
def test_crd_wait_polls_until_established(installation, monkeypatch, status):
    states = iter(
        [
            {"status": status},
            {"status": {"conditions": [{"type": "Established", "status": "True"}]}},
        ]
    )
    reads = []
    sleeps = []

    def get(kind, name):
        reads.append((kind, name))
        return next(states)

    monkeypatch.setattr(installation.runner, "get", get)
    monkeypatch.setattr("enkictl.lifecycle.time.sleep", sleeps.append)

    installation.wait_crd("widgets.example.com")

    assert reads == [("customresourcedefinition", "widgets.example.com")] * 2
    assert sleeps == [1]


@pytest.mark.parametrize(
    "conditions",
    [
        None,
        [],
        [
            {
                "type": "Established",
                "status": "False",
                "reason": "NotAccepted",
                "message": "name conflict",
            }
        ],
    ],
)
def test_crd_wait_timeout_reports_last_conditions(installation, monkeypatch, conditions):
    monkeypatch.setattr(
        installation.runner, "get", lambda *_: {"status": {"conditions": conditions}}
    )

    with pytest.raises(StackError) as error:
        installation.wait_crd("widgets.example.com", timeout=0)

    assert "CRD widgets.example.com did not reach Established=True after 0s" in str(error.value)
    assert json.dumps(conditions or []) in str(error.value)


def test_crd_wait_reports_disappeared_resource(installation, monkeypatch):
    monkeypatch.setattr(installation.runner, "get", lambda *_: None)

    with pytest.raises(StackError, match="widgets.example.com disappeared"):
        installation.wait_crd("widgets.example.com")


def test_crd_wait_propagates_api_failure(installation, monkeypatch):
    def get(*_):
        raise StackError("Forbidden: cannot get customresourcedefinitions")

    monkeypatch.setattr(installation.runner, "get", get)

    with pytest.raises(StackError, match="Forbidden"):
        installation.wait_crd("widgets.example.com")


def test_apply_preserves_kserve_podspec(installation):
    template = {"containers": [{"name": "worker", "image": "example/worker:test"}]}
    doc = resource(
        "LLMInferenceServiceConfig",
        "worker",
        "kserve",
        apiVersion="serving.kserve.io/v1alpha2",
        spec={"template": copy.deepcopy(template)},
    )
    installation.versions = {"serving.kserve.io/v1alpha2"}

    installation.apply(doc, "kserve")

    applied = installation.runner.get("llminferenceserviceconfig", "worker", "kserve")
    assert applied["spec"]["template"] == template
    assert applied["metadata"]["labels"][LABEL] == installation.state["id"]
    assert installation.state["resources"][0]["status"] == "applied"


def test_crd_wait_stops_at_deadline(installation, monkeypatch):
    elapsed = [0.0]
    sleeps = []

    def sleep(duration):
        sleeps.append(duration)
        elapsed[0] += duration

    monkeypatch.setattr(installation.runner, "get", lambda *_: {"status": {"conditions": None}})
    monkeypatch.setattr("enkictl.lifecycle.time.monotonic", lambda: elapsed[0])
    monkeypatch.setattr("enkictl.lifecycle.time.sleep", sleep)

    with pytest.raises(StackError, match="after 120s"):
        installation.wait_crd("widgets.example.com")

    assert elapsed[0] == 120
    assert sleeps == [1] * 120


def test_apply_waits_for_crd_with_null_initial_conditions(installation, monkeypatch):
    doc = resource(
        "CustomResourceDefinition",
        "widgets.example.com",
        apiVersion="apiextensions.k8s.io/v1",
        status={"conditions": None},
    )
    cluster = installation.runner

    def establish(_duration):
        record = installation.state["resources"][0]
        assert record["status"] == "pending"
        live = cluster.get("customresourcedefinition", "widgets.example.com")
        live["status"]["conditions"] = [{"type": "Established", "status": "True"}]
        cluster.put(live)

    monkeypatch.setattr("enkictl.lifecycle.time.sleep", establish)

    installation.apply(doc, "kserve")

    assert installation.state["resources"][0]["status"] == "applied"
    assert not any(call[0] == "wait" for call in cluster.calls)


@pytest.fixture
def conflict_installation(installation):
    installation.deployment.components = []
    installation.deployment.acting = []
    return installation


def test_conflicts_batches_by_type_and_namespace(conflict_installation, monkeypatch):
    installation = conflict_installation
    cluster = installation.runner
    for namespace in ("dex", "observability"):
        cluster.put(resource("Namespace", namespace))
    docs = [
        resource(kind, f"test-{index}", namespace)
        for namespace in ("dex", "observability")
        for kind in ("ConfigMap", "Secret")
        for index in range(25)
    ]
    for doc in docs:
        cluster.put(mark(doc, installation.state["id"]))
    installation.deployment.resources["auth"] = docs
    original_get = cluster.get

    def get(kind, name, namespace=""):
        assert (kind, namespace) in {
            ("namespace", ""),
            ("configmap", "kube-system"),
        }, "Conflict resources must be listed in batches, not fetched individually"
        return original_get(kind, name, namespace)

    monkeypatch.setattr(cluster, "get", get)
    installation.conflicts()

    lists = [call[1] for call in cluster.calls if call[0] == "read"]
    assert lists == [
        ("get", "namespace", "kube-system"),
        ("get", "namespaces"),
        ("get", "configmap", "-n", "dex"),
        ("get", "secret", "-n", "dex"),
        ("get", "configmap", "-n", "observability"),
        ("get", "secret", "-n", "observability"),
    ]


def test_conflicts_skips_missing_namespaces_but_checks_cluster_resources(
    conflict_installation,
):
    installation = conflict_installation
    installation.deployment.resources["auth"] = [resource("ConfigMap", "test", "absent")]
    release = Release("test", "absent", "chart", "1", "auth", [], "auth")
    installation.deployment.releases = [release]
    installation.deployment.templates["test"] = [
        resource("Secret", "test"),
        resource("ClusterRole", "foreign", apiVersion="rbac.authorization.k8s.io/v1"),
    ]
    installation.runner.put(installation.deployment.templates["test"][1])
    installation.versions = {"v1", "rbac.authorization.k8s.io/v1"}

    with pytest.raises(StackError, match="Foreign resource conflict: ClusterRole/foreign"):
        installation.conflicts()

    calls = installation.runner.calls
    assert ("read", ("get", "clusterrole.rbac.authorization.k8s.io")) in calls
    assert not any("-n" in call[1] or call[0] == "helm" for call in calls)


@pytest.mark.parametrize("replaced", [False, True])
def test_batched_conflicts_preserves_ownership_checks(conflict_installation, replaced):
    installation = conflict_installation
    installation.runner.put(resource("Namespace", "dex"))
    doc = resource("ConfigMap", "test", "dex")
    installation.deployment.resources["auth"] = [doc]
    if replaced:
        doc = mark(doc, installation.state["id"])
        installation.state["resources"].append({**identity(doc), "uid": "old"})
    installation.runner.put(doc)

    with pytest.raises(StackError, match="replaced" if replaced else "Foreign resource"):
        installation.conflicts()


def test_conflict_batches_are_not_reused_after_check(conflict_installation):
    installation = conflict_installation
    installation.runner.put(resource("Namespace", "dex"))
    doc = resource("ConfigMap", "test", "dex")
    installation.deployment.resources["auth"] = [doc]
    installation.conflicts()
    installation.runner.put(doc)

    assert installation.live(doc) is not None
    with pytest.raises(StackError, match="Foreign resource"):
        installation.conflicts()


def test_conflicts_batches_helm_releases_and_resources(conflict_installation):
    installation = conflict_installation
    cluster = installation.runner
    cluster.put(resource("Namespace", "dex"))
    installation.deployment.resources["auth"] = [resource("ConfigMap", "local", "dex")]
    for name in ("first", "second"):
        installation.deployment.releases.append(
            Release(name, "dex", "chart", "1", "auth", [], "auth")
        )
        installation.deployment.templates[name] = [
            resource("ConfigMap", name),
            resource("Namespace", "ignored"),
            resource("Job", "hook", apiVersion="batch/v1"),
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"generateName": "job-"}},
        ]
        hook = installation.deployment.templates[name][2]
        hook["metadata"]["annotations"] = {"helm.sh/hook": "pre-install"}
        cluster.put(resource("Job", "hook", "dex", apiVersion="batch/v1"))
    installation.versions = {"v1", "batch/v1"}
    installation.conflicts()

    assert len([call for call in cluster.calls if call[0] == "helm"]) == 1
    assert cluster.calls.count(("read", ("get", "configmap", "-n", "dex"))) == 1
    assert not any(
        call[0] == "read" and call[1][1] in {"job.batch", "namespace"} and len(call[1]) == 2
        for call in cluster.calls
    )


def test_conflicts_separates_api_groups_but_reuses_versions(conflict_installation, monkeypatch):
    installation = conflict_installation
    cluster = installation.runner
    cluster.put(resource("Namespace", "dex"))
    installation.versions = {"first.io/v1", "first.io/v2", "second.io/v1"}
    docs = [
        resource("Widget", "test", "dex", apiVersion=version)
        for version in ("first.io/v1", "first.io/v2", "second.io/v1")
    ]
    installation.deployment.resources["auth"] = docs
    original_json = cluster.json
    reads = []

    def read(*args):
        if args[1].startswith("widget."):
            reads.append(args)
            doc = docs[0] if args[1] == "widget.first.io" else docs[2]
            return {"items": [mark(doc, installation.state["id"])]}
        return original_json(*args)

    monkeypatch.setattr(cluster, "json", read)
    installation.conflicts()

    assert reads == [
        ("get", "widget.first.io", "-n", "dex"),
        ("get", "widget.second.io", "-n", "dex"),
    ]


def test_conflicts_rejects_foreign_helm_release(conflict_installation, monkeypatch):
    installation = conflict_installation
    installation.runner.put(resource("Namespace", "dex"))
    installation.deployment.releases = [Release("foreign", "dex", "chart", "1", "auth", [], "auth")]
    monkeypatch.setattr(installation.runner, "helm", lambda *_: '[{"name": "foreign"}]')

    with pytest.raises(StackError, match="Foreign Helm release"):
        installation.conflicts()


def test_conflicts_skips_unserved_api_versions(conflict_installation):
    installation = conflict_installation
    installation.runner.put(resource("Namespace", "dex"))
    installation.deployment.resources["auth"] = [
        resource("Unknown", "test", "dex", apiVersion="not-installed.io/v1")
    ]
    installation.conflicts()

    assert [call[1] for call in installation.runner.calls if call[0] == "read"] == [
        ("get", "namespace", "kube-system"),
        ("get", "namespaces"),
    ]


@pytest.mark.parametrize("compatible", [False, True])
def test_conflicts_preserves_shared_crd_validation(conflict_installation, compatible):
    installation = conflict_installation
    doc = resource(
        "CustomResourceDefinition",
        "widgets.example.com",
        apiVersion="apiextensions.k8s.io/v1",
        spec={
            "group": "example.com",
            "scope": "Namespaced",
            "versions": [{"name": "v1", "schema": {"openAPIV3Schema": {"type": "object"}}}],
        },
    )
    installation.deployment.resources["auth"] = [doc]
    live = copy.deepcopy(doc)
    if not compatible:
        live["spec"]["versions"][0]["schema"] = {}
    installation.runner.put(live)

    if compatible:
        installation.conflicts()
    else:
        with pytest.raises(StackError, match="Shared CRD schema differs"):
            installation.conflicts()
    assert (
        "read",
        ("get", "customresourcedefinition.apiextensions.k8s.io"),
    ) in installation.runner.calls


@pytest.mark.parametrize("matching", [False, True])
def test_conflicts_reuses_secret_batch_for_credentials(
    conflict_installation, monkeypatch, matching
):
    installation = conflict_installation
    cluster = installation.runner
    cluster.put(resource("Namespace", "dex"))
    doc = resource(
        "Secret",
        "grafana-admin",
        "dex",
        data={"admin-user": "YWRtaW4=", "admin-password": "cGFzc3dvcmQ="},
    )
    cluster.put(mark(doc, installation.state["id"]))
    monkeypatch.setattr(installation, "credential_names", lambda: [("dex", "grafana-admin")])
    monkeypatch.setattr(
        installation,
        "credential_values",
        lambda: [
            (
                "dex",
                "grafana-admin",
                {"admin-user": "admin", "admin-password": "password" if matching else "changed"},
            )
        ],
    )
    original_get = cluster.get

    def get(kind, name, namespace=""):
        assert kind != "secret", "Credential checks must reuse the Secret batch"
        return original_get(kind, name, namespace)

    monkeypatch.setattr(cluster, "get", get)

    if matching:
        installation.conflicts()
    else:
        with pytest.raises(StackError, match="Credential mismatch"):
            installation.conflicts()
    assert cluster.calls.count(("read", ("get", "secret", "-n", "dex"))) == 1


def test_conflicts_skips_secret_checks_in_missing_namespaces(conflict_installation, monkeypatch):
    installation = conflict_installation
    monkeypatch.setattr(installation, "generated_secret_names", lambda: [("absent", "tls")])
    monkeypatch.setattr(installation, "credential_names", lambda: [("absent", "grafana-admin")])
    monkeypatch.setattr(
        installation, "credential_values", lambda: [("absent", "grafana-admin", {})]
    )
    original_get = installation.runner.get

    def get(kind, name, namespace=""):
        assert namespace != "absent"
        return original_get(kind, name, namespace)

    monkeypatch.setattr(installation.runner, "get", get)
    installation.conflicts()

    assert not any("-n" in call[1] for call in installation.runner.calls)


def test_conflicts_missing_dependency_namespace_still_fails(conflict_installation):
    installation = conflict_installation
    installation.deployment.components = ["auth"]
    installation.state["resources"] = [
        {
            **identity(resource("ConfigMap", "test", "absent")),
            "component": "auth",
            "phase": "auth",
            "status": "applied",
        }
    ]

    with pytest.raises(StackError, match="Dependency auth is missing resources"):
        installation.conflicts()
    assert not any("-n" in call[1] for call in installation.runner.calls)


def test_conflicts_batches_dependency_checks(conflict_installation, monkeypatch):
    installation = conflict_installation
    cluster = installation.runner
    cluster.put(resource("Namespace", "dex"))
    installation.deployment.components = ["auth"]
    docs = [
        cluster.put(mark(resource("ConfigMap", name, "dex"), installation.state["id"]))
        for name in ("first", "second")
    ]
    installation.state["resources"] = [
        {**identity(doc), "component": "auth", "phase": "auth", "status": "applied"} for doc in docs
    ]
    ready = []
    monkeypatch.setattr(installation, "wait_resources", ready.extend)
    installation.conflicts()

    assert ready == docs
    assert cluster.calls.count(("read", ("get", "configmap", "-n", "dex"))) == 1


def test_conflicts_propagates_batch_read_errors(conflict_installation, monkeypatch):
    installation = conflict_installation
    installation.runner.put(resource("Namespace", "dex"))
    installation.deployment.resources["auth"] = [resource("ConfigMap", "test", "dex")]
    original_json = installation.runner.json

    def read(*args):
        if args == ("get", "configmap", "-n", "dex"):
            raise StackError("Injected list failure")
        return original_json(*args)

    monkeypatch.setattr(installation.runner, "json", read)
    with pytest.raises(StackError, match="Injected list failure"):
        installation.conflicts()


def test_apply_tracks_ownership_and_uid(installation):
    installation.apply(resource("ConfigMap", "test", "dex", data={"x": "y"}), "auth")
    record = installation.state["resources"][0]
    assert record["status"] == "applied" and record["uid"]
    live = installation.runner.get("configmap", "test", "dex")
    assert live["metadata"]["labels"][LABEL] == installation.state["id"]
    assert yaml.safe_load(installation.path.read_text())["resources"][0]["uid"] == record["uid"]


def test_foreign_resource_refused_without_writes(installation):
    installation.runner.put(resource("ConfigMap", "test", "dex"))
    with pytest.raises(StackError, match="Foreign resource"):
        installation.apply(resource("ConfigMap", "test", "dex"), "auth")
    assert not any(call[0] == "apply" for call in installation.runner.calls)


def test_replaced_resource_refused(installation):
    doc = resource("ConfigMap", "test", "dex")
    installation.apply(doc, "auth")
    installation.runner.objects[("configmap", "dex", "test")]["metadata"]["uid"] = "replacement"
    with pytest.raises(StackError, match="replaced"):
        installation.apply(doc, "auth")


def test_partial_failure_inventory_is_recoverable(installation):
    installation.runner.fail_apply = "fail"
    with pytest.raises(StackError, match="Injected"):
        installation.apply(resource("ConfigMap", "fail", "dex"), "auth")
    record = yaml.safe_load(installation.path.read_text())["resources"][0]
    assert record["status"] == "pending"
    assert "uid" not in record


def test_legacy_generated_inventory_is_read_and_migrated_to_profile_yaml(installation):
    installation.legacy_path.parent.mkdir(parents=True)
    installation.legacy_path.write_text(json.dumps(installation.state))

    migrated = Installation(installation.deployment)

    assert migrated.state["id"] == installation.state["id"]
    assert migrated.exists
    assert not migrated.path.exists()
    migrated.save()
    assert yaml.safe_load(migrated.path.read_text())["id"] == installation.state["id"]
    assert installation.legacy_path.exists()


def test_uninstall_preserves_data_and_credentials(installation):
    installation.bind(write=True)
    for kind, name in [
        ("ConfigMap", "config"),
        ("Secret", "credentials"),
        ("PersistentVolumeClaim", "data"),
    ]:
        installation.apply(resource(kind, name, "dex"), "auth")
    installation.uninstall(False)
    assert installation.runner.get("configmap", "config", "dex") is None
    assert installation.runner.get("secret", "credentials", "dex") is not None
    assert installation.runner.get("persistentvolumeclaim", "data", "dex") is not None
    assert installation.state["status"] == "uninstalled"


def test_purge_after_uninstall_removes_retained_data(installation):
    installation.bind(write=True)
    installation.apply(resource("Secret", "credentials", "dex"), "auth")
    installation.apply(resource("PersistentVolumeClaim", "data", "dex"), "auth")
    installation.uninstall(False)
    installation.uninstall(True)
    assert installation.runner.get("secret", "credentials", "dex") is None
    assert installation.runner.get("persistentvolumeclaim", "data", "dex") is None


def test_cluster_mismatch_blocks_deletion(installation):
    installation.state["cluster_uid"] = "wrong"
    installation.save()
    with pytest.raises(StackError, match="different cluster"):
        installation.uninstall(True)
    assert not any(call[0] == "delete" for call in installation.runner.calls)


def test_install_phases_are_ordered(installation, monkeypatch):
    calls = []
    monkeypatch.setattr(installation, "credentials", lambda _: calls.append("credentials"))
    monkeypatch.setattr(installation, "capture_generated", lambda: None)
    monkeypatch.setattr(installation, "wait_phase", lambda phase: calls.append(phase))
    installation.install()
    assert [call for call in calls if call != "credentials"] == PHASES
    assert calls.index("credentials") < calls.index("auth")
    assert installation.state["status"] == "installed"


def test_postrenderer_retention_and_pod_labels():
    doc = mark(
        resource(
            "StatefulSet",
            "db",
            spec={
                "template": {"spec": {}},
                "volumeClaimTemplates": [{"metadata": {"name": "data"}}],
            },
        ),
        "installation",
    )
    assert doc["spec"]["persistentVolumeClaimRetentionPolicy"]["whenDeleted"] == "Retain"
    assert doc["spec"]["template"]["metadata"]["labels"][LABEL] == "installation"
    for kind in ("Secret", "PersistentVolumeClaim", "CustomResourceDefinition", "Namespace"):
        assert (
            mark(resource(kind, "keep"), "id")["metadata"]["annotations"]["helm.sh/resource-policy"]
            == "keep"
        )


def test_purge_preserves_crd_with_foreign_instances(installation, monkeypatch):
    installation.bind(write=True)
    doc = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "widgets.example.org"},
        "spec": {"group": "example.org", "names": {"plural": "widgets"}},
    }
    live = installation.runner.put(mark(doc, installation.state["id"]))
    installation.applied(installation.intend(identity(doc), "gateway"), live)
    installation.runner.foreign_instances = [{"metadata": {"name": "foreign"}}]
    monkeypatch.setattr(installation, "capture_generated", lambda: None)
    assert "instances still exist" in installation.uninstall(True)[0]
    assert installation.runner.get("customresourcedefinition", "widgets.example.org")


def test_custom_cluster_scope_is_derived_from_crds(installation):
    installation.deployment.templates["test"] = [
        {
            "kind": "CustomResourceDefinition",
            "spec": {
                "scope": "Cluster",
                "names": {"kind": "NodeFeatureRule"},
            },
        }
    ]
    assert installation.default_namespace({"kind": "NodeFeatureRule"}, "gpu-operator") == ""


def test_topology_change_requires_uninstall_but_does_not_block_removal(installation):
    installation.bind(write=True)
    installation.state["status"] = "installed"
    installation.config.profile.domain = "changed.customer.net"
    with pytest.raises(StackError, match="topology migration"):
        installation.bind(configuration=True)
    installation.bind()


def test_missing_local_credentials_do_not_rotate_existing_secrets(installation):
    installation.apply(resource("Secret", "existing", "dex"), "auth")
    with pytest.raises(StackError, match="Restore missing"):
        installation.credentials()


def test_generated_certificate_secrets_inherit_installation_identity():
    cert = mark(resource("Certificate", "tls", spec={"secretName": "tls"}), "identity")
    assert cert["spec"]["secretTemplate"]["labels"][LABEL] == "identity"


def test_failed_issuance_retains_unverified_generated_key_with_warning(installation):
    installation.bind(write=True)
    doc = resource("Secret", "enki-acme-production", "cert-manager")
    installation.intend(identity(doc), "certificate-generated", component="certificates")
    installation.runner.put(doc)
    warnings = installation.uninstall(True)
    assert any("unverified generated Secret" in warning for warning in warnings)
    assert installation.runner.get("secret", "enki-acme-production", "cert-manager")


def test_addon_removal_is_separate_and_retains_then_purges_data(installation):
    installation.deployment.components = []
    installation.deployment.acting = []
    installation.bind(write=True)
    installation.state["addons"][NAME] = {"status": "installed"}
    installation.component = OWNER
    for doc in (
        resource("ConfigMap", "addon-config", "librechat"),
        resource("Secret", "addon-secret", "librechat"),
        resource("PersistentVolumeClaim", "addon-data", "librechat"),
    ):
        installation.apply(doc, "addon")

    assert installation.removal_targets() == []
    preview = installation.addon_removal_preview(NAME)
    assert {item[1] for item in preview} == {"ConfigMap", "Secret", "PersistentVolumeClaim"}
    assert {item[0] for item in preview} == {"remove", "retain"}
    installation.state["components"]["auth"] = {"status": "installed"}

    installation.uninstall_addon(NAME)
    assert installation.runner.get("configmap", "addon-config", "librechat") is None
    assert installation.runner.get("secret", "addon-secret", "librechat") is not None
    assert installation.runner.get("persistentvolumeclaim", "addon-data", "librechat") is not None
    assert installation.state["status"] == "installed"

    installation.uninstall_addon(NAME, purge=True)
    assert installation.runner.get("secret", "addon-secret", "librechat") is None
    assert installation.runner.get("persistentvolumeclaim", "addon-data", "librechat") is None
    assert installation.state["addons"][NAME]["status"] == "purged"


@pytest.mark.parametrize("name", ["librechat", "searxng", "crawl4ai"])
def test_generated_addon_resources_keep_inventory_owner(installation, name, monkeypatch):
    installation.bind(write=True)
    installation.state["addons"][name] = {"status": "installed"}
    generated = mark(
        resource("Secret", "generated", name), installation.state["id"], f"addon:{name}"
    )
    generated = installation.runner.put(generated)
    original_json = installation.runner.json
    monkeypatch.setattr(
        installation.runner,
        "json",
        lambda *args: (
            {"items": [generated]}
            if args == ("get", "secrets", "--all-namespaces")
            else original_json(*args)
        ),
    )

    installation.capture_generated()

    record = next(r for r in installation.state["resources"] if r["name"] == "generated")
    assert record["component"] == f"addon:{name}"
