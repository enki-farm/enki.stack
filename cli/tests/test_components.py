import copy
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_lifecycle import Cluster
from typer.testing import CliRunner

from enkictl.components import REGISTRY, defaults, removal, resolve
from enkictl.components.base import COMPONENT_LABEL
from enkictl.config import Configuration, StackError, dump, private_write, read_yaml, stack_root
from enkictl.lifecycle import Installation, identity
from enkictl.main import app, prepare
from enkictl.postrender import LABEL, mark
from enkictl.render import Deployment, resource

ROOT = Path(__file__).resolve().parents[2]


def managed(config, tmp_path, targets):
    d = Deployment(config, ROOT, Cluster(), targets)
    d.root = tmp_path
    return Installation(d)


def test_dependency_closure_and_defaults():
    assert resolve(["auth", "kserve", "auth"]) == [
        "certificates",
        "ai-gateway",
        "auth",
        "kserve",
    ]
    assert resolve(["gpu-operator"]) == ["gpu-operator"]
    assert "gpu-operator" not in defaults("existing")
    assert "gpu-operator" in defaults("operator")
    assert "kserve" not in resolve(["kubeflow-hub"])


def test_invalid_graph_and_unknown_targets(monkeypatch):
    with pytest.raises(StackError, match="Unknown component"):
        resolve(["typo"])
    monkeypatch.setitem(
        REGISTRY, "certificates", replace(REGISTRY["certificates"], dependencies=("auth",))
    )
    with pytest.raises(StackError, match="cycle"):
        resolve(["auth"])


def test_removal_preserves_dependencies_and_orders_dependents():
    assert removal(["monitoring"], ["auth", "monitoring"]) == ["monitoring"]
    assert removal(["auth", "monitoring"], ["auth", "monitoring"]) == ["monitoring", "auth"]
    with pytest.raises(StackError, match="monitoring"):
        removal(["auth"], ["auth", "monitoring"])


def test_gpu_only_profile_and_assets_are_isolated(tmp_path):
    path = tmp_path / "profile.yaml"
    private_write(
        path,
        dump(
            {
                "platform": "dgx-spark",
                "context": "never-contact",
                "gpu": "operator",
            }
        ),
    )
    root = tmp_path / "stack"
    (root / "helm/gpu-operator").mkdir(parents=True)
    shutil.copyfile(ROOT / "helm/platform.yaml", root / "helm/platform.yaml")
    config = Configuration(path, targets=["gpu-operator"])
    config.validate()
    assert config.connector == {}
    assert stack_root(root, ["gpu-operator"]) == root
    d, _ = prepare(path, root, charts=False, targets=["gpu-operator"])
    assert not any(d.resources.values())
    assert [r.name for r in d.releases] == ["gpu-operator"]
    result = CliRunner().invoke(
        app, ["validate", "gpu-operator", "-p", str(path), "--stack-root", str(root)]
    )
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(
        app, ["component", "list", "-p", str(path), "--stack-root", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "live health is not checked" in result.output
    assert "  none" in result.output


def test_gpu_operator_rejects_incompatible_intent(profile_path):
    config = Configuration(profile_path, targets=["gpu-operator"])
    config.profile.gpu = "existing"
    with pytest.raises(StackError, match="gpu: operator"):
        config.validate()


def test_auth_without_consumer_groups_or_hub_assets(profile_path, tmp_path):
    values = Configuration(profile_path).profile.model_dump()
    values["auth"] = {"connector": "connector.yaml"}
    private_write(profile_path, dump(values))
    config = Configuration(profile_path, targets=["auth"])
    config.validate()
    d = Deployment(config, ROOT, Cluster(), ["auth"])
    # The fake cluster has no Kustomize executable; prove only auth assets are requested.
    loaded = []
    original_load = d.load

    def load(path, kustomize=False):
        loaded.append(path)
        if path == "k8s/dex":
            return [
                resource("PersistentVolumeClaim", "dex", "dex", spec={}),
                resource("Deployment", "dex", "dex", spec={"template": {"spec": {}}}),
            ]
        return original_load(path, kustomize)

    d.load = load
    d.compose()
    assert d.resources["auth"][0]["kind"] == "ConfigMap"
    assert d.resources["auth"][0]["metadata"]["name"] == "dex-config"
    deployment = next(doc for doc in d.resources["auth"] if doc["kind"] == "Deployment")
    assert deployment["spec"]["template"]["metadata"]["annotations"]["checksum/dex-config"]
    assert not any("kubeflow" in path or "monitoring" in path for path in loaded)
    assert not d.resources["hub"] and not d.resources["content"]
    d.root = tmp_path
    installation = Installation(d)
    installation.credentials("auth")
    assert set(config.secrets.generated) == {"grafana", "hub", "librechat"}
    assert installation.credential_names() == [("dex", "dex-secrets")]
    assert installation.runner.get("secret", "dex-secrets", "dex")
    assert not installation.runner.get("secret", "model-registry-db-secrets", "kubeflow")
    assert all(r["component"] == "auth" for r in installation.state["resources"])


def test_installed_dependencies_are_not_reconciled(config, tmp_path):
    installation = managed(config, tmp_path, ["monitoring"])
    for name in resolve(["auth"]):
        installation.state["components"][name] = {"status": "installed"}
    installation.plan()
    assert installation.deployment.acting == ["monitoring"]
    assert all(r.component == "monitoring" for r in installation.deployment.releases)
    config.configure(installation.deployment.acting)
    assert not config.connector and not config.references
    assert "auth" in installation.deployment.installed


@pytest.mark.parametrize("status", ["installed", "failed", "installing", "removing", "new"])
def test_pending_dependents_block_removal(config, tmp_path, status):
    installation = managed(config, tmp_path, ["monitoring"])
    installation.state["components"] = {
        "auth": {"status": "installed"},
        "monitoring": {"status": status},
    }
    with pytest.raises(StackError, match="monitoring"):
        installation.removal_targets(["auth"])
    assert installation.runner.calls == []


def test_selected_purge_does_not_touch_auth_or_credentials(config, tmp_path):
    installation = managed(config, tmp_path, ["monitoring"])
    installation.bind(write=True)
    installation.apply(resource("ConfigMap", "auth", "dex"), "auth")
    installation.apply(resource("Secret", "oauth", "dex"), "auth")
    installation.apply(resource("ConfigMap", "grafana", "observability"), "content")
    installation.apply(resource("Secret", "oauth", "observability"), "monitoring")
    installation.apply(resource("PersistentVolumeClaim", "data", "observability"), "monitoring")
    for name in installation.state["components"]:
        installation.state["components"][name]["status"] = "installed"
    before = copy.deepcopy(installation.runner.objects)
    generated = dict(config.secrets.generated)
    installation.uninstall(True, ["monitoring"])
    assert installation.runner.get("configmap", "grafana", "observability") is None
    assert installation.runner.get("secret", "oauth", "observability") is None
    assert installation.runner.get("persistentvolumeclaim", "data", "observability") is None
    for key in [("configmap", "dex", "auth"), ("secret", "dex", "oauth")]:
        assert installation.runner.objects[key] == before[key]
    assert installation.state["components"]["auth"]["status"] == "installed"
    assert installation.state["status"] == "installed"
    assert config.secrets.generated == generated


def test_remove_reinstall_consumer_keeps_oauth_values(config, tmp_path):
    installation = managed(config, tmp_path, ["monitoring"])
    installation.bind(write=True)
    installation.credentials()
    before = dict(config.secrets.generated)
    installation.uninstall(False, ["monitoring"])
    installation.credentials("monitoring")
    assert config.secrets.generated == before
    assert installation.runner.get("secret", "dex-secrets", "dex")
    assert installation.runner.get("secret", "oidc-credentials", "observability")


def test_legacy_inventory_is_rejected_without_writes(config, tmp_path):
    installation = managed(config, tmp_path, ["auth"])
    installation.state["schema_version"] = 1
    installation.save()
    before = installation.path.read_text()
    with pytest.raises(StackError, match="Unsupported inventory schema"):
        Installation(installation.deployment)
    assert installation.path.read_text() == before
    assert not installation.runner.calls


def test_generated_pvc_inherits_component_from_owner_chain(config, tmp_path):
    installation = managed(config, tmp_path, ["monitoring"])
    parent = installation.runner.put(
        mark(
            resource("Deployment", "grafana", "observability"),
            installation.state["id"],
            "monitoring",
        )
    )
    installation.applied(installation.intend(identity(parent), "monitoring"), parent)
    replica = resource("ReplicaSet", "grafana-rs", "observability")
    replica["metadata"].update(uid="rs", ownerReferences=[{"uid": parent["metadata"]["uid"]}])
    pod = resource("Pod", "grafana-pod", "observability")
    pod["metadata"].update(uid="pod", ownerReferences=[{"uid": "rs"}])
    pvc = resource("PersistentVolumeClaim", "data", "observability")
    pvc["metadata"].update(uid="pvc", ownerReferences=[{"uid": "pod"}])
    installation.runner.put(pvc)
    original = installation.runner.json
    installation.runner.json = lambda *args: (
        {
            "items": [
                replica,
                pod,
                installation.runner.get("persistentvolumeclaim", "data", "observability"),
            ]
        }
        if args[1] == "persistentvolumeclaims"
        else original(*args)
    )
    installation.capture_generated()
    record = next(r for r in installation.state["resources"] if r["name"] == "data")
    assert record["component"] == "monitoring"
    assert record["uid"]


def test_component_labels_propagate_to_pods_and_persistent_data():
    doc = mark(
        resource(
            "StatefulSet",
            "db",
            spec={"template": {}, "volumeClaimTemplates": [{"metadata": {"name": "data"}}]},
        ),
        "install",
        "kubeflow-hub",
    )
    assert doc["spec"]["template"]["metadata"]["labels"][COMPONENT_LABEL] == "kubeflow-hub"
    assert (
        doc["spec"]["volumeClaimTemplates"][0]["metadata"]["labels"][COMPONENT_LABEL]
        == "kubeflow-hub"
    )
    assert doc["metadata"]["labels"][LABEL] == "install"


def test_failure_records_explicit_component_intent(config, tmp_path):
    installation = managed(config, tmp_path, ["auth"])
    installation.runner.fail_apply = "failed"
    with pytest.raises(StackError, match="Injected"):
        installation.apply(resource("ConfigMap", "failed", "dex"), "auth")
    record = read_yaml(installation.path)["resources"][0]
    assert record["status"] == "pending" and record["component"] == "auth"


def test_unknown_cli_target_fails_before_profile_access():
    result = CliRunner().invoke(app, ["component", "install", "typo", "-p", "/not/a/profile"])
    assert result.exit_code == 1
    assert "Unknown component: typo" in result.output


def test_gpu_init_collects_no_auth_tls_or_domain(tmp_path):
    path = tmp_path / "profile.yaml"
    result = CliRunner().invoke(
        app,
        ["init", "--component", "gpu-operator", "-p", str(path)],
        input="dgx-spark\nnever-contact\noperator\ngpu-operator\nnone\nsave\n",
    )
    assert result.exit_code == 0, result.output
    config = Configuration(path, targets=["gpu-operator"])
    config.validate()
    assert config.profile.domain is None and config.profile.auth is None
    assert not (tmp_path / "connector.yaml").exists()
    assert "Base domain" not in result.output and "Certificate mode" not in result.output


def test_incremental_engine_install_remove_reinstall(config, tmp_path, monkeypatch):
    installation = managed(config, tmp_path, ["auth"])
    d = installation.deployment
    d.releases = []
    d.resources["cert-manager"] = [resource("ConfigMap", "cert-ready", "cert-manager")]
    d.resources["gateway"] = [resource("ConfigMap", "gateway-ready", d.gateway_namespace)]
    d.resources["auth"] = [resource("ConfigMap", "dex-config", "dex")]
    monkeypatch.setattr(installation, "capture_generated", lambda: None)
    monkeypatch.setattr(installation, "wait_phase", lambda _: None)
    installation.install()
    assert all(item["status"] == "installed" for item in installation.state["components"].values())
    assert not installation.runner.get("namespace", "observability")
    assert not installation.runner.get("namespace", "kubeflow")
    oauth = {key: config.secrets.generated[key] for key in ("grafana", "hub")}

    next_d = Deployment(config, ROOT, installation.runner, ["monitoring"])
    next_d.root = tmp_path
    second = Installation(next_d)
    second.plan()
    assert next_d.acting == ["monitoring"]
    config.configure(next_d.acting)
    config.validate()
    next_d.releases = []
    next_d.resources["monitoring"] = [resource("ConfigMap", "grafana", "observability")]
    second.conflicts()
    monkeypatch.setattr(second, "capture_generated", lambda: None)
    monkeypatch.setattr(second, "wait_phase", lambda _: None)
    second.install()
    with pytest.raises(StackError, match="monitoring"):
        second.uninstall(False, ["auth"])
    second.uninstall(False, ["monitoring"])
    assert second.state["components"]["auth"]["status"] == "installed"
    assert second.runner.get("configmap", "dex-config", "dex")
    second.plan()
    second.install()
    assert second.state["components"]["monitoring"]["status"] == "installed"
    assert all(config.secrets.generated[key] == value for key, value in oauth.items())


@pytest.mark.parametrize("dry_run", [False, True])
def test_component_install_command_persists_inventory(config, tmp_path, monkeypatch, dry_run):
    from enkictl import main

    installation = managed(config, tmp_path, ["auth"])
    deployment = installation.deployment
    deployment.releases = []
    installation.state["desired"] = {"components": [], "addons": []}
    installation.state["addons"]["librechat"] = {"status": "installed"}
    installation.save()
    before = installation.path.read_text()
    monkeypatch.setattr(installation, "capture_generated", lambda: None)
    monkeypatch.setattr(installation, "wait_phase", lambda _: None)
    monkeypatch.setattr(main, "checked", lambda *_: (deployment, installation))

    result = CliRunner().invoke(
        app,
        ["component", "install", "auth", "-p", str(config.path), "--yes"]
        + (["--dry-run"] if dry_run else []),
    )

    assert result.exit_code == 0, result.output
    saved = read_yaml(installation.path)
    assert saved["addons"] == {"librechat": {"status": "installed"}}
    assert saved["desired"] == {"components": [], "addons": []}
    reloaded = main.local_inventory(config.path, None)
    assert reloaded.state == saved
    listed = CliRunner().invoke(app, ["component", "list", "-p", str(config.path)])
    assert listed.exit_code == 0, listed.output
    if dry_run:
        assert installation.path.read_text() == before
        assert saved["components"] == {}
        assert "  none" in listed.output
    else:
        for name in deployment.components:
            assert saved["components"][name]["status"] == "installed"
            assert f"  {name}: installed\n" in listed.output


def test_finished_components_stay_installed_after_later_failure(config, tmp_path, monkeypatch):
    installation = managed(config, tmp_path, ["monitoring"])
    installation.deployment.releases = []
    installation.deployment.resources["monitoring"] = [
        resource("ConfigMap", "fail", "observability")
    ]
    installation.runner.fail_apply = "fail"
    monkeypatch.setattr(installation, "capture_generated", lambda: None)
    monkeypatch.setattr(installation, "wait_phase", lambda _: None)
    with pytest.raises(StackError, match="Injected"):
        installation.install()
    assert installation.state["components"]["auth"]["status"] == "installed"
    assert installation.state["components"]["monitoring"]["status"] == "installing"


def test_missing_shared_oauth_never_regenerates_for_consumer(config, tmp_path):
    installation = managed(config, tmp_path, ["monitoring"])
    for name in resolve(["auth"]):
        installation.state["components"][name] = {"status": "installed"}
    installation.plan()
    with pytest.raises(StackError, match="Restore missing generated"):
        installation.credentials("monitoring")
    assert config.secrets.generated == {}


@pytest.mark.parametrize("exclude_crds", [[], ["shared.example.test"]])
def test_helm_reinstall_accepts_new_uid_after_owned_deletion(config, tmp_path, exclude_crds):
    installation = managed(config, tmp_path, ["monitoring"])
    installation.component = "monitoring"
    doc = resource("ConfigMap", "grafana", "observability")
    installation.apply(doc, "monitoring")
    record = installation.state["resources"][0]
    original_uid = record["uid"]
    installation.delete(record)
    installation.deployment.templates["grafana"] = [doc]
    installation.runner.post_renderer = lambda: "test-renderer"

    def helm(*args, data=None):
        assert installation.runner.helm_exclude_crds == exclude_crds
        if args[0] == "upgrade":
            installation.runner.put(
                mark(
                    doc,
                    installation.state["id"],
                    "monitoring",
                )
            )
        if args[:2] == ("get", "manifest"):
            return dump(doc)
        return "[]"

    installation.runner.helm = helm
    installation.helm_install(
        SimpleNamespace(
            name="grafana",
            namespace="observability",
            component="monitoring",
            phase="monitoring",
            exclude_crds=exclude_crds,
            args=lambda *_: [],
        )
    )
    assert record["status"] == "applied"
    assert record["uid"] != original_uid
    assert installation.runner.helm_exclude_crds == []


def test_dependency_verifies_gateway_readiness_without_writes(config, tmp_path):
    installation = managed(config, tmp_path, ["auth"])
    doc = resource("Gateway", "gateway", "envoy-ai-gateway-system")
    installation.apply(doc, "gateway")
    installation.runner.calls.clear()
    installation.verify_dependency("ai-gateway")
    calls = installation.runner.calls
    assert any(call[0] == "wait" and "--for=condition=Programmed" in call[1] for call in calls)
    assert not any(call[0] in {"apply", "label", "patch", "delete"} for call in calls)


def test_full_install_cannot_silently_change_existing_gpu_mode(config, tmp_path):
    installation = managed(config, tmp_path, None)
    installation.bind(write=True)
    installation.state["components"]["gpu-operator"]["status"] = "installed"
    config.profile.gpu = "existing"
    with pytest.raises(StackError, match="uninstalling gpu-operator"):
        installation.bind(configuration=True)
