import json
import subprocess
import tarfile
from base64 import b64encode
from pathlib import Path

import pytest
from test_resources import assert_container_resources
from typer.testing import CliRunner

from enkictl.addons import REGISTRY
from enkictl.addons.activepieces import (
    CHART_VERSION,
    GATEWAY_SCRIPT,
    NAME,
    SECRET,
    ActivepiecesAddon,
)
from enkictl.checks import requests
from enkictl.config import StackError, dump, read_yaml
from enkictl.main import addon_order, app
from enkictl.render import documents

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def addon(profile_path, monkeypatch):
    route = (ROOT / "k8s/addons/activepieces/httproute.yaml").read_text()
    policy = (ROOT / "k8s/addons/activepieces/securitypolicy.yaml").read_text()
    monkeypatch.setattr(
        "enkictl.addons.internal.Runner.render", lambda *_: f"{route}\n---\n{policy}"
    )
    monkeypatch.setattr("enkictl.addons.internal.Runner.get", lambda *_: None)
    return ActivepiecesAddon(profile_path, ROOT)


def test_registry_and_profile_configuration(addon):
    assert REGISTRY[NAME] is ActivepiecesAddon
    assert addon_order([NAME]) == [NAME]
    assert addon.dependencies == ("ai-gateway", "auth")
    assert addon.deployment.components == []
    assert addon.deployment.resources["content"][0]["spec"]["hostnames"] == ["ap.platform.internal"]
    routes = [doc for doc in addon.deployment.resources["content"] if doc["kind"] == "HTTPRoute"]
    assert {route["metadata"]["name"] for route in routes} == {
        "activepieces",
        "activepieces-webhooks",
    }
    webhook_route = next(
        route for route in routes if route["metadata"]["name"] == "activepieces-webhooks"
    )
    assert webhook_route["spec"]["rules"][0]["matches"][0]["path"]["value"] == ("/api/v1/webhooks")
    assert (
        "X-Auth-Request-Email"
        in webhook_route["spec"]["rules"][0]["filters"][0]["requestHeaderModifier"]["remove"]
    )
    assert (
        "X-Auth-Request-Groups"
        in webhook_route["spec"]["rules"][0]["filters"][0]["requestHeaderModifier"]["remove"]
    )
    policy = next(
        doc for doc in addon.deployment.resources["content"] if doc["kind"] == "SecurityPolicy"
    )
    assert policy["spec"]["targetRefs"][0]["name"] == "activepieces"
    assert policy["spec"]["oidc"]["clientID"] == "activepieces"
    assert policy["spec"]["oidc"]["forwardIDToken"] == {
        "header": "X-Envoy-Activepieces-ID-Token"
    }
    assert "forwardAccessToken" not in policy["spec"]["oidc"]
    assert policy["spec"]["jwt"]["providers"][0]["extractFrom"]["headers"] == [
        {"name": "X-Envoy-Activepieces-ID-Token"}
    ]
    assert policy["spec"]["jwt"]["providers"][0]["claimToHeaders"] == [
        {"claim": "email", "header": "X-Auth-Request-Email"},
        {"claim": "name", "header": "X-Auth-Request-User"},
        {"claim": "preferred_username", "header": "X-Auth-Request-Preferred-Username"},
    ]
    app_route = next(route for route in routes if route["metadata"]["name"] == "activepieces")
    assert app_route["spec"]["rules"][0]["filters"][0]["requestHeaderModifier"]["remove"] == [
        "X-Envoy-Activepieces-ID-Token"
    ]
    values = addon.deployment.overrides[NAME]
    assert values["activepiecesConfig"]["AP_FRONTEND_URL"] == ("https://ap.platform.internal")
    assert values["image"] == {
        "repository": "ghcr.io/enki-farm/activepieces",
        "tag": "latest",
        "pullPolicy": "Always",
    }
    assert values["activepiecesConfig"]["AP_PROXY_AUTH_ENABLED"] == "true"
    assert values["activepiecesConfig"]["AP_PROXY_AUTH_BOOTSTRAP_ADMIN_EMAIL"] == (
        "marco@enki.swiss"
    )
    assert values["persistence"]["storageClassName"] == "local-path"
    assert values["postgresql"]["auth"]["existingSecret"] == SECRET
    assert values["redis"]["auth"]["existingSecret"] == SECRET
    assert values["kubernetes-secret-generator"]["enabled"] is False


def test_packaged_chart_matches_upstream_values():
    with tarfile.open(ROOT / f"helm/activepieces/activepieces-{CHART_VERSION}.tgz") as chart:
        assert (
            chart.extractfile("activepieces/values.yaml").read()
            == (ROOT / "helm/activepieces/values.yaml").read_bytes()
        )
        assert chart.extractfile("activepieces/LICENSE").read()
        metadata = documents(chart.extractfile("activepieces/Chart.yaml").read().decode())[0]
        assert metadata["version"] == CHART_VERSION
        assert metadata["appVersion"] == "0.92.2"


def test_credentials_generate_and_reuse_without_writing(addon, monkeypatch):
    values = addon._credentials()
    assert len(values["AP_ENCRYPTION_KEY"]) == 32
    assert all(len(values[key]) == 64 for key in addon.secret_keys[1:])
    assert addon.config.secrets.addons == {}
    monkeypatch.setattr(
        addon.runner,
        "get",
        lambda *_: {
            "data": {key: b64encode(value.encode()).decode() for key, value in values.items()}
        },
    )
    assert addon._credentials() == values
    addon.config.secrets.addons[NAME] = {"AP_POSTGRES_PASSWORD": "changed"}
    with pytest.raises(StackError, match="credential differs"):
        addon._credentials()


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("AP_ENCRYPTION_KEY", "not-hex", "32 hexadecimal"),
        ("AP_JWT_SECRET", "short", "at least 32"),
    ],
)
def test_credentials_reject_invalid_keys(addon, key, value, message):
    addon.config.secrets.addons[NAME] = {key: value}
    with pytest.raises(StackError, match=message):
        addon._credentials()


def test_missing_domain_or_tls_is_rejected(profile_path, monkeypatch):
    profile = read_yaml(profile_path)
    profile.pop("domain")
    profile_path.write_text(dump(profile))
    monkeypatch.setattr("enkictl.addons.internal.Runner.render", lambda *_: "")
    with pytest.raises(StackError, match="requires profile domain and tls"):
        ActivepiecesAddon(profile_path, ROOT)


def prepare_check(addon, monkeypatch):
    addon.installation.state["components"]["ai-gateway"] = {"status": "installed"}
    addon.installation.state["components"]["auth"] = {"status": "installed"}
    monkeypatch.setattr(addon.installation, "verify_dependency", lambda *_: None)
    monkeypatch.setattr(addon.runner, "kubectl", lambda *_: "")

    def get(kind, name, namespace=""):
        if name == "dex-secrets":
            return {"data": {"ACTIVEPIECES_CLIENT_SECRET": b64encode(b"dex-key").decode()}}
        return None

    monkeypatch.setattr(addon.runner, "get", get)
    monkeypatch.setattr("enkictl.addons.internal.check_prerequisites", lambda *_, **__: [])
    monkeypatch.setattr(addon.installation, "conflicts", lambda: None)


def test_check_requires_gateway_and_includes_credential_ownership(addon, monkeypatch):
    monkeypatch.setattr(addon.runner, "kubectl", lambda *_: "")
    with pytest.raises(StackError, match="installed ai-gateway"):
        addon.check()
    prepare_check(addon, monkeypatch)
    seen = []

    def prerequisites(deployment, progress=None):
        assert deployment.acting == ["addon:activepieces"]
        assert deployment.resources["credentials"][0]["metadata"]["name"] == SECRET
        assert deployment.resources["content"][0]["metadata"]["name"] == "activepieces-oidc"
        seen.append("preflight")
        return []

    monkeypatch.setattr("enkictl.addons.internal.check_prerequisites", prerequisites)
    values, _, warnings = addon.check()
    assert seen == ["preflight"]
    assert values.keys() == set(addon.secret_keys)
    assert addon.deployment.acting == []
    assert addon.config.secrets.addons == {}
    assert any("unsandboxed" in warning for warning in warnings)


def test_check_requires_activepieces_dex_client_secret(addon, monkeypatch):
    prepare_check(addon, monkeypatch)
    monkeypatch.setattr(addon.runner, "get", lambda *_: None)
    with pytest.raises(StackError, match="component install auth"):
        addon.check()


@pytest.mark.parametrize("fail", [False, True])
def test_install_persists_credentials_and_tracks_helm_failure(addon, monkeypatch, fail):
    prepare_check(addon, monkeypatch)
    values, password, _ = addon.check()
    calls = []
    monkeypatch.setattr(addon.installation, "bind", lambda **_: calls.append("bind"))
    monkeypatch.setattr(
        addon.installation,
        "apply",
        lambda doc, _: calls.append(doc["kind"]),
    )

    def helm_install(release):
        assert release.component == "addon:activepieces"
        assert release.version == CHART_VERSION
        calls.append("helm")
        if fail:
            raise StackError("rollout failed")

    monkeypatch.setattr(addon.installation, "helm_install", helm_install)
    monkeypatch.setattr(addon.installation, "wait_resources", lambda _: calls.append("wait"))
    monkeypatch.setattr(addon.installation, "capture_generated", lambda: calls.append("capture"))
    monkeypatch.setattr(addon, "_configure_gateway", lambda: calls.append("gateway"))
    if fail:
        with pytest.raises(StackError, match="partial inventory retained.*rollout failed"):
            addon.install(values, password)
        assert calls[-1] == "helm"
        assert "wait" not in calls
    else:
        addon.install(values, password)
        assert calls == [
            "bind",
            "Namespace",
            "Secret",
            "Secret",
            "HTTPRoute",
            "HTTPRoute",
            "SecurityPolicy",
            "helm",
            "wait",
            "capture",
            "gateway",
        ]
    state = read_yaml(addon.installation.path)
    assert state["addons"][NAME]["status"] == ("failed" if fail else "installed")
    assert state["components"] == {
        "ai-gateway": {"status": "installed"},
        "auth": {"status": "installed"},
    }
    assert addon.installation.component is None
    assert read_yaml(addon.config.secret_path)["addons"][NAME] == values
    assert addon.config.secret_path.stat().st_mode & 0o077 == 0


def test_cli_dry_run_does_not_persist(addon, monkeypatch):
    from enkictl import main

    prepare_check(addon, monkeypatch)
    monkeypatch.setitem(main.ADDONS, NAME, lambda **_: addon)
    monkeypatch.setattr(addon, "install", lambda *_: pytest.fail("dry-run installed addon"))
    before = addon.config.secret_path.read_bytes()
    result = CliRunner().invoke(
        app, ["addon", "install", NAME, "-p", str(addon.config.path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "no cluster changes" in result.output
    assert addon.config.secret_path.read_bytes() == before
    assert not addon.installation.path.exists()


def test_install_waits_for_platform_trust_before_helm(addon, monkeypatch):
    prepare_check(addon, monkeypatch)
    values, _, _ = addon.check()
    calls = []
    monkeypatch.setattr(addon.installation, "bind", lambda **_: None)
    monkeypatch.setattr(addon.installation, "apply", lambda *_: None)
    monkeypatch.setattr(addon.installation, "wait_resources", lambda *_: None)
    monkeypatch.setattr(addon.installation, "capture_generated", lambda: None)
    monkeypatch.setattr(addon, "_configure_gateway", lambda: None)
    monkeypatch.setattr(addon.runner, "kubectl", lambda *args: calls.append(args))
    monkeypatch.setattr(addon.installation, "helm_install", lambda _: calls.append(("helm",)))
    addon.install(values)
    assert calls == [
        ("label", "namespace", NAME, "enki.stack/trust=true", "--overwrite"),
        (
            "wait",
            "configmap/enki-platform-trust",
            "-n",
            NAME,
            "--for=jsonpath={.data.ca\\.crt}",
            "--timeout=180s",
        ),
        ("helm",),
    ]


@pytest.mark.parametrize("platform", ["dgx-spark", "aks"])
def test_official_chart_renders_persistent_workloads_and_required_secrets(
    profile_path, monkeypatch, platform
):
    profile_path.write_text(
        profile_path.read_text().replace("dgx-spark", platform).replace("operator", "existing")
    )
    route = (ROOT / "k8s/addons/activepieces/httproute.yaml").read_text()
    monkeypatch.setattr("enkictl.addons.internal.Runner.render", lambda *_: route)
    addon = ActivepiecesAddon(profile_path, ROOT)
    addon.runner.installation = "test-render-only"
    addon.deployment.render_charts("1.34.0")
    docs = addon.deployment.templates[NAME]
    assert_container_resources(docs, NAME)
    workloads = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "StatefulSet"}
    assert set(workloads) == {NAME, "activepieces-postgresql", "activepieces-redis-master"}
    assert not any(
        doc["kind"] in {"Rollout", "ClusterRole", "CustomResourceDefinition"} for doc in docs
    )
    for workload in workloads.values():
        requests(workload["spec"]["template"]["spec"])
    for doc in workloads.values():
        assert doc["metadata"]["labels"]["enki.stack/component"] == "addon-activepieces"
        assert doc["spec"]["persistentVolumeClaimRetentionPolicy"]["whenDeleted"] == "Retain"
        for claim in doc["spec"]["volumeClaimTemplates"]:
            assert claim["spec"]["storageClassName"] == addon.config.profile.storage_class
            assert claim["metadata"]["labels"]["enki.stack/installation"] == "test-render-only"
    pod = workloads[NAME]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == "ghcr.io/enki-farm/activepieces:latest"
    assert container["imagePullPolicy"] == "Always"
    assert (
        workloads["activepieces-postgresql"]["spec"]["template"]["spec"]["containers"][0]["image"]
        == "docker.io/bitnamilegacy/postgresql:14.9.0-debian-11-r61"
    )
    assert (
        workloads["activepieces-redis-master"]["spec"]["template"]["spec"]["containers"][0]["image"]
        == "docker.io/bitnamilegacy/redis:7.0.15-debian-12-r20"
    )
    env = {item["name"]: item for item in container["env"]}
    assert len(env) == len(container["env"])
    assert env["AP_REDIS_TYPE"]["value"] == "STANDALONE"
    assert env["AP_PROXY_AUTH_ENABLED"]["value"] == "true"
    assert env["AP_PROXY_AUTH_BOOTSTRAP_ADMIN_EMAIL"]["value"] == "marco@enki.swiss"
    for key in addon.secret_keys:
        assert env[key]["valueFrom"]["secretKeyRef"] == {
            "name": SECRET,
            "key": key,
            "optional": True,
        }
    assert env["NODE_EXTRA_CA_CERTS"]["value"] == "/etc/enki-trust/ca.crt"
    assert any(
        volume.get("configMap", {}).get("name") == "enki-platform-trust"
        for volume in pod["volumes"]
    )
    for doc in docs:
        if doc["kind"] == "Secret":
            assert doc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_gateway_requires_local_api_key(addon):
    with pytest.raises(StackError, match="addons.activepieces.ACTIVEPIECES_API_KEY"):
        addon._configure_gateway()


@pytest.mark.parametrize("gateway_key", [None, "gateway-test-key"])
def test_gateway_uses_profile_origin_and_stdin_credentials(addon, monkeypatch, gateway_key):
    local = {"ACTIVEPIECES_API_KEY": "admin-test-key"}
    if gateway_key:
        local["ENKI_GATEWAY_API_KEY"] = gateway_key
    addon.config.secrets.addons[NAME] = local
    calls = []
    monkeypatch.setattr(
        addon.runner, "kubectl", lambda *args, data: calls.append((args, json.loads(data)))
    )
    addon._configure_gateway()
    args, payload = calls[0]
    assert args == (
        "exec", "-i", "-n", NAME, "statefulset/activepieces", "--", "node", "-e", GATEWAY_SCRIPT,
    )
    assert "admin-test-key" not in " ".join(args)
    assert payload["token"] == "admin-test-key"
    provider = payload["provider"]
    assert provider["provider"] == "custom"
    assert provider["displayName"] == "Enki Gateway"
    assert provider["auth"] == {"apiKey": "Bearer " + (gateway_key or "sk-local-vllm")}
    assert provider["config"] == {
        "baseUrl": "https://ai.platform.internal/v1",
        "apiKeyHeader": "Authorization",
        "apiStyle": "chat",
        "models": [{"modelId": "default", "modelName": "Enki", "modelType": "text"}],
    }
    assert "ACTIVEPIECES_API_KEY" not in addon.secret_keys


@pytest.mark.parametrize(
    "scenario",
    ["create", "update", "conflict", "duplicate", "http-error", "bad-shape", "not-saved"],
)
def test_gateway_script_provisions_and_verifies_provider(scenario):
    harness = """
const scenario = SCENARIO;
const calls = [];
const provider = {
    displayName: 'Enki Gateway', provider: 'custom',
    auth: { apiKey: 'Bearer gateway-test-key' },
    config: {
        baseUrl: 'https://ai.platform.internal/v1', apiKeyHeader: 'Authorization',
        models: [{ modelId: 'default', modelName: 'Enki', modelType: 'text' }],
    },
};
const row = { id: 'existing-id', name: provider.displayName, provider: 'custom',
    config: provider.config };
const other = { id: 'other-id', name: 'Other provider', provider: 'openai' };
let saved = false;
global.fetch = async (url, options) => {
    calls.push({ url, ...options, signal: undefined });
    if (scenario === 'http-error') {
        return { ok: false, status: 401 };
    }
    let result;
    if (options.method === 'POST') {
        saved = true;
        result = row;
    } else if (scenario === 'bad-shape') {
        result = { data: [] };
    } else if (saved && scenario !== 'not-saved') {
        result = [other, row];
    } else if (scenario === 'update') {
        result = [other, row];
    } else if (scenario === 'conflict') {
        result = [{ ...row, provider: 'openai' }];
    } else if (scenario === 'duplicate') {
        result = [row, row];
    } else {
        result = [other];
    }
    return { ok: true, json: async () => result };
};
process.on('beforeExit', () => console.log(JSON.stringify(calls)));
""".replace("SCENARIO", json.dumps(scenario))
    payload = {
        "token": "admin-test-key",
        "provider": {
            "displayName": "Enki Gateway",
            "provider": "custom",
            "auth": {"apiKey": "Bearer gateway-test-key"},
            "config": {
                "baseUrl": "https://ai.platform.internal/v1",
                "apiKeyHeader": "Authorization",
                "models": [{"modelId": "default", "modelName": "Enki", "modelType": "text"}],
            },
        },
    }
    result = subprocess.run(
        ["node", "-e", harness + GATEWAY_SCRIPT],
        input=json.dumps(payload), text=True, capture_output=True, timeout=10,
    )
    calls = json.loads(result.stdout)
    assert calls[0]["url"] == "http://127.0.0.1:80/api/v1/ai-providers/configs"
    assert calls[0]["headers"]["Authorization"] == "Bearer admin-test-key"
    if scenario in {"create", "update"}:
        assert result.returncode == 0, result.stderr
        assert len(calls) == 3
        assert calls[1]["method"] == "POST"
        assert calls[1]["url"].endswith("/existing-id" if scenario == "update" else "/ai-providers")
        body = json.loads(calls[1]["body"])
        assert body["config"] == payload["provider"]["config"]
        assert ("provider" in body) == (scenario == "create")
        assert "projectScope" not in body
        assert "enabledForChat" not in body
    else:
        assert result.returncode == 1
        assert result.stderr
        assert "admin-test-key" not in result.stderr
        assert "gateway-test-key" not in result.stderr
        if scenario != "not-saved":
            assert len(calls) == 1


def test_gateway_failure_retains_failed_install_inventory(addon, monkeypatch):
    prepare_check(addon, monkeypatch)
    values, _, _ = addon.check()
    monkeypatch.setattr(addon.installation, "bind", lambda **_: None)
    monkeypatch.setattr(addon.installation, "apply", lambda *_: None)
    monkeypatch.setattr(addon.installation, "helm_install", lambda *_: None)
    monkeypatch.setattr(addon.installation, "wait_resources", lambda *_: None)
    monkeypatch.setattr(addon.installation, "capture_generated", lambda: None)
    with pytest.raises(StackError, match="partial inventory retained.*ACTIVEPIECES_API_KEY"):
        addon.install(values)
    assert read_yaml(addon.installation.path)["addons"][NAME]["status"] == "failed"
