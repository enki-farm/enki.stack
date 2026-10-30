from base64 import b64encode
from pathlib import Path

import pytest
from test_lifecycle import Cluster

from enkictl.addons.librechat import LibreChatAddon
from enkictl.config import StackError, dump, private_write, read_yaml
from enkictl.lifecycle import Installation
from enkictl.render import Deployment, resource

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def addon(profile_path, monkeypatch):
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    return LibreChatAddon(profile_path, ROOT)


def test_librechat_requires_auth_component(addon):
    assert "auth" in addon.dependencies


def test_dex_only_login_and_group_restriction(addon):
    env = addon.deployment.overrides["librechat"]["librechat"]["configEnv"]
    assert env["ALLOW_EMAIL_LOGIN"] == "false"
    assert env["ALLOW_EMAIL_LOGIN_OVERRIDE"] == "false"
    assert env["ALLOW_REGISTRATION"] == "false"
    assert env["ALLOW_SOCIAL_LOGIN"] == "true"
    assert env["ALLOW_SOCIAL_REGISTRATION"] == "true"
    assert env["ALLOW_SHARED_LINKS_PUBLIC"] == "false"
    assert env["OPENID_CLIENT_ID"] == "librechat"
    assert env["OPENID_ISSUER"] == "https://auth.platform.internal"
    assert env["OPENID_REQUIRED_ROLE"] == "test:core"
    assert env["OPENID_REQUIRED_ROLE_TOKEN_KIND"] == "id"
    assert env["OPENID_REQUIRED_ROLE_PARAMETER_PATH"] == "groups"
    assert env["OPENID_USE_PKCE"] == env["OPENID_GENERATE_NONCE"] == "true"
    assert env["OPENID_AUTO_REDIRECT"] == "true"
    content = addon.deployment.overrides["librechat"]["librechat"]["configYamlContent"]
    from enkictl.render import documents

    assert documents(content)[0]["registration"]["socialLogins"] == ["openid"]


def test_explicit_chat_group_overrides_hub_group(addon, profile_path):
    profile = read_yaml(profile_path)
    profile["auth"]["chat_group"] = "chat:members"
    private_write(profile_path, dump(profile))
    addon = LibreChatAddon(profile_path, ROOT)
    env = addon.deployment.overrides["librechat"]["librechat"]["configEnv"]
    assert env["OPENID_REQUIRED_ROLE"] == "chat:members"


def test_missing_access_group_fails_closed(addon, profile_path):
    profile = read_yaml(profile_path)
    profile["auth"]["hub_group"] = ""
    private_write(profile_path, dump(profile))
    with pytest.raises(StackError, match="requires auth.chat_group"):
        LibreChatAddon(profile_path, ROOT)


def test_self_signed_mounts_ca_without_disabling_tls(addon):
    values = addon.deployment.overrides["librechat"]
    env = values["librechat"]["configEnv"]
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/enki-trust/ca.crt"
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in env
    assert {"name": "platform-trust", "configMap": {"name": "enki-platform-trust"}} in values[
        "volumes"
    ]
    assert {"name": "platform-trust", "mountPath": "/etc/enki-trust", "readOnly": True} in values[
        "volumeMounts"
    ]


def test_missing_dex_secret_has_migration_guidance(addon, monkeypatch):
    monkeypatch.setattr(addon.runner, "get", lambda *_: None)
    with pytest.raises(StackError, match="enkictl component install auth"):
        addon._credentials()


def test_oidc_secret_drift_is_rejected(addon, monkeypatch):
    def get(kind, name, namespace=""):
        if name == "dex-secrets":
            return {"data": {"LIBRECHAT_CLIENT_SECRET": b64encode(b"dex-key").decode()}}
        if name == "librechat-credentials-env":
            return {"data": {"OPENID_CLIENT_SECRET": b64encode(b"wrong-key").decode()}}
        return None

    monkeypatch.setattr(addon.runner, "get", get)
    with pytest.raises(StackError, match="differs from Dex"):
        addon._credentials()


def test_missing_dex_client_fails_before_render_or_upgrade(addon, monkeypatch):
    for name in addon.dependencies:
        addon.installation.state["components"][name] = {"status": "installed"}
    monkeypatch.setattr(addon.installation, "verify_dependency", lambda *_: None)
    monkeypatch.setattr(addon.runner, "get", lambda *_: None)
    with pytest.raises(StackError, match="callback is not registered"):
        addon.preflight()


@pytest.fixture
def existing_auth(config, tmp_path):
    config.secrets.generated.update(grafana="grafana-key", hub="hub-key")
    deployment = Deployment(config, ROOT, Cluster(), ["auth"])
    deployment.root = tmp_path
    deployment.acting = ["auth"]
    installation = Installation(deployment)
    installation.component = "auth"
    installation.state["components"]["auth"] = {"status": "installed"}
    installation.state["credentials"] = {
        "grafana": {"component": "auth"},
        "hub": {"component": "auth"},
    }
    installation.apply(
        resource(
            "Secret",
            "dex-secrets",
            "dex",
            data={
                "GRAFANA_CLIENT_SECRET": b64encode(b"grafana-key").decode(),
                "HUB_CLIENT_SECRET": b64encode(b"hub-key").decode(),
            },
        ),
        "auth",
    )
    return installation


def test_existing_auth_can_add_librechat_client_without_rotating_other_keys(existing_auth):
    existing_auth.credentials("auth")
    generated = existing_auth.config.secrets.generated
    assert generated["grafana"] == "grafana-key"
    assert generated["hub"] == "hub-key"
    assert len(generated["librechat"]) == 64
    first = generated["librechat"]
    existing_auth.credentials("auth")
    assert generated["librechat"] == first
    assert "librechat" in existing_auth.state["credentials"]


def test_migration_recovers_existing_dex_client_secret(existing_auth):
    live = existing_auth.runner.get("secret", "dex-secrets", "dex")
    live["data"]["LIBRECHAT_CLIENT_SECRET"] = b64encode(b"existing-client").decode()
    existing_auth.runner.put(live)
    existing_auth.credentials("auth")
    assert existing_auth.config.secrets.generated["librechat"] == "existing-client"


def test_registered_client_without_secret_is_not_silently_rotated(existing_auth):
    existing_auth.runner.put(
        resource(
            "ConfigMap",
            "dex-config",
            "dex",
            data={"config.yaml": dump({"staticClients": [{"id": "librechat"}]})},
        )
    )
    with pytest.raises(StackError, match="refusing rotation"):
        existing_auth.credentials("auth")


def test_tracked_missing_client_secret_requires_restore(existing_auth):
    existing_auth.state["credentials"]["librechat"] = {"component": "auth"}
    with pytest.raises(StackError, match="Restore missing generated credential: librechat"):
        existing_auth.credentials("auth")
