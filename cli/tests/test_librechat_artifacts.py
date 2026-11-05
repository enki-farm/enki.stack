from base64 import b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from test_lifecycle import Cluster

from enkictl.addons.librechat import BUNDLER_SECRET, OWNER, LibreChatAddon
from enkictl.config import StackError, dump, private_write, read_yaml
from enkictl.lifecycle import Installation
from enkictl.render import Deployment, documents, resource

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def addon(profile_path, monkeypatch):
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    return LibreChatAddon(profile_path, ROOT)


def test_both_bundlers_use_profile_https_origin(addon):
    env = addon.deployment.overrides["librechat"]["librechat"]["configEnv"]
    assert env["SANDPACK_BUNDLER_URL"] == "https://bundler.platform.internal"
    assert env["SANDPACK_STATIC_BUNDLER_URL"] == "https://preview.bundler.platform.internal"
    assert env["CSP_FRAME_SRC_EXTRA"].split() == [
        env["SANDPACK_BUNDLER_URL"],
        "https://*.bundler.platform.internal",
    ]
    config = documents(addon.deployment.overrides["librechat"]["librechat"]["configYamlContent"])[0]
    assert "capabilities" not in config["endpoints"].get("agents", {})


def test_bundler_is_internal_pinned_and_nonroot(addon):
    content = addon.deployment.resources["content"]
    service = next(doc for doc in content if doc["kind"] == "Service")
    assert service["spec"]["type"] == "ClusterIP"
    assert not service["spec"].get("externalIPs")
    workload = next(doc for doc in content if doc["kind"] == "Deployment")
    spec = workload["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    container = spec["containers"][0]
    assert container["image"].startswith("ghcr.io/librechat-ai/codesandbox-client/bundler@sha256:")
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["readinessProbe"]["httpGet"]["path"] == "/index.html"
    assert workload["spec"]["template"]["metadata"]["annotations"]["checksum/bundler-config"]
    network = next(doc for doc in content if doc["kind"] == "NetworkPolicy")
    assert network["spec"]["podSelector"]["matchLabels"] == service["spec"]["selector"]
    assert network["spec"]["ingress"][0]["from"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "envoy-gateway-system"}
            }
        }
    ]
    config = next(doc for doc in content if doc["kind"] == "ConfigMap")
    assert "Access-Control-Allow-Origin" not in config["data"]["nginx.conf"]
    assert 'Cache-Control "private, no-store"' in config["data"]["nginx.conf"]


def test_bundler_route_is_https_and_group_restricted(addon):
    policy, route, static_route = addon.deployment.resources["bundler-routes"]
    spec = policy["spec"]
    assert route["spec"]["hostnames"] == ["bundler.platform.internal"]
    assert route["spec"]["parentRefs"][0]["sectionName"] == "https"
    assert spec["targetRefs"][0]["name"] == route["metadata"]["name"]
    assert spec["oidc"]["clientID"] == "bundler"
    assert spec["oidc"]["clientSecret"]["name"] == BUNDLER_SECRET
    assert spec["oidc"]["redirectURL"] == "https://bundler.platform.internal/oauth2/callback"
    assert spec["oidc"]["cookieDomain"] == "bundler.platform.internal"
    assert static_route["spec"]["hostnames"] == ["*.bundler.platform.internal"]
    assert static_route["spec"]["rules"][0]["backendRefs"][0]["port"] == 81
    assert spec["targetRefs"][1]["name"] == static_route["metadata"]["name"]
    assert spec["jwt"]["providers"][0]["audiences"] == ["bundler"]
    assert spec["jwt"]["providers"][0]["extractFrom"] == {
        "headers": [{"name": "X-Envoy-Bundler-ID-Token"}]
    }
    assert spec["authorization"]["defaultAction"] == "Deny"
    assert spec["authorization"]["rules"][0]["principal"]["jwt"]["claims"] == [
        {"name": "groups", "valueType": "StringArray", "values": ["test:core"]}
    ]
    filters = route["spec"]["rules"][0]["filters"]
    assert "X-Envoy-Bundler-ID-Token" in filters[0]["requestHeaderModifier"]["remove"]
    headers = {
        entry["name"]: entry["value"] for entry in filters[1]["responseHeaderModifier"]["set"]
    }
    assert headers["Content-Security-Policy"] == "frame-ancestors https://chat.platform.internal"
    assert headers["Cache-Control"] == "private, no-store"


def test_custom_namespace_and_chat_group_apply_to_all_bundler_resources(addon, profile_path):
    profile = read_yaml(profile_path)
    profile["auth"]["chat_group"] = "chat:members"
    private_write(profile_path, dump(profile))
    addon = LibreChatAddon(profile_path, ROOT, namespace="private-chat")
    for docs in addon.deployment.resources.values():
        assert all(doc["metadata"]["namespace"] == "private-chat" for doc in docs)
    policy = addon.deployment.resources["bundler-routes"][0]
    assert policy["spec"]["authorization"]["rules"][0]["principal"]["jwt"]["claims"][0][
        "values"
    ] == ["chat:members"]


def test_self_signed_policy_uses_platform_ca_for_oidc_and_jwks(addon):
    backend = next(doc for doc in addon.deployment.resources["content"] if doc["kind"] == "Backend")
    assert backend["spec"]["tls"]["sni"] == "auth.platform.internal"
    assert backend["spec"]["tls"]["caCertificateRefs"][0]["name"] == "enki-platform-trust"
    spec = addon.deployment.resources["bundler-routes"][0]["spec"]
    refs = spec["oidc"]["provider"]["backendRefs"]
    assert refs[0]["name"] == backend["metadata"]["name"]
    assert spec["jwt"]["providers"][0]["remoteJWKS"]["backendRefs"] == refs


@pytest.mark.parametrize("wildcard", [True, False])
def test_static_preview_requires_nested_wildcard_certificate(addon, monkeypatch, wildcard):
    domain = addon.config.profile.domain
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    names = [domain, f"*.{domain}"]
    if wildcard:
        names.append(f"*.bundler.{domain}")
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(value) for value in names]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    secret = {
        "type": "kubernetes.io/tls",
        "data": {
            "tls.crt": b64encode(cert.public_bytes(serialization.Encoding.PEM)).decode(),
            "tls.key": b64encode(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            ).decode(),
        },
    }
    monkeypatch.setattr(addon.runner, "get", lambda *_: secret)
    if wildcard:
        addon._check_bundler_tls()
    else:
        with pytest.raises(StackError, match="component install certificates"):
            addon._check_bundler_tls()


@pytest.fixture
def dex_credentials(addon, monkeypatch):
    data = {
        "LIBRECHAT_CLIENT_SECRET": b64encode(b"chat-client").decode(),
        "BUNDLER_CLIENT_SECRET": b64encode(b"bundler-client").decode(),
    }

    def get(kind, name, namespace=""):
        if name == "dex-secrets":
            return {"data": data}
        if name == "crawl4ai-credentials":
            return {"data": {"CRAWL4AI_API_TOKEN": b64encode(b"crawl-token").decode()}}
        return None

    monkeypatch.setattr(addon.runner, "get", get)
    return data


def test_bundler_secret_is_prepared_without_leaking_into_chat_env(addon, dex_credentials):
    values, _ = addon._credentials()
    (secret,) = addon.deployment.resources["credentials"]
    assert secret["metadata"]["name"] == BUNDLER_SECRET
    assert secret["metadata"]["namespace"] == addon.namespace
    assert secret["data"]["client-secret"] == dex_credentials["BUNDLER_CLIENT_SECRET"]
    assert "BUNDLER_CLIENT_SECRET" not in values
    assert not addon.config.secrets.addons["librechat"]


def test_missing_bundler_secret_requires_auth_upgrade(addon, dex_credentials):
    dex_credentials.pop("BUNDLER_CLIENT_SECRET")
    with pytest.raises(StackError, match="Dex bundler client is not configured"):
        addon._credentials()


def test_bundler_secret_drift_is_rejected(addon, dex_credentials, monkeypatch):
    get = addon.runner.get
    monkeypatch.setattr(
        addon.runner,
        "get",
        lambda kind, name, namespace="": (
            {"data": {"client-secret": b64encode(b"wrong-client").decode()}}
            if name == BUNDLER_SECRET
            else get(kind, name, namespace)
        ),
    )
    with pytest.raises(StackError, match="Bundler OIDC client credential differs from Dex"):
        addon._credentials()


def test_bundler_callback_is_required_before_install(addon, monkeypatch):
    for dependency in addon.dependencies:
        addon.installation.state["components"][dependency] = {"status": "installed"}
    monkeypatch.setattr(addon.installation, "verify_dependency", lambda *_: None)
    monkeypatch.setattr(
        addon.runner,
        "get",
        lambda *_: {
            "data": {
                "config.yaml": dump(
                    {
                        "staticClients": [
                            {
                                "id": "librechat",
                                "secretEnv": "LIBRECHAT_CLIENT_SECRET",
                                "redirectURIs": [
                                    "https://chat.platform.internal/oauth/openid/callback"
                                ],
                            }
                        ]
                    }
                )
            }
        },
    )
    with pytest.raises(StackError, match="Dex bundler callback is not registered"):
        addon.preflight()


@pytest.fixture
def old_auth(config, tmp_path):
    config.secrets.generated.update(
        grafana="grafana-key", hub="hub-key", librechat="chat-key", activepieces="ap-key"
    )
    deployment = Deployment(config, ROOT, Cluster(), ["auth"])
    deployment.root = tmp_path
    deployment.acting = ["auth"]
    installation = Installation(deployment)
    installation.component = "auth"
    installation.apply(
        resource(
            "Secret",
            "dex-secrets",
            "dex",
            data={
                f"{name.upper()}_CLIENT_SECRET": b64encode(value.encode()).decode()
                for name, value in config.secrets.generated.items()
            },
        ),
        "auth",
    )
    return installation


def test_auth_upgrade_adds_stable_bundler_key_without_rotating_existing_keys(old_auth):
    before = dict(old_auth.config.secrets.generated)
    old_auth.credentials("auth")
    generated = old_auth.config.secrets.generated
    assert all(generated[name] == value for name, value in before.items())
    assert len(generated["bundler"]) == 64
    first = generated["bundler"]
    old_auth.credentials("auth")
    assert generated["bundler"] == first
    assert old_auth.state["credentials"]["bundler"] == {"component": "auth"}


def test_auth_migration_recovers_existing_bundler_credential(old_auth):
    live = old_auth.runner.get("secret", "dex-secrets", "dex")
    live["data"]["BUNDLER_CLIENT_SECRET"] = b64encode(b"existing-bundler-key").decode()
    old_auth.runner.put(live)
    old_auth.credentials("auth")
    assert old_auth.config.secrets.generated["bundler"] == "existing-bundler-key"


def test_registered_bundler_without_secret_refuses_rotation(old_auth):
    old_auth.runner.put(
        resource(
            "ConfigMap",
            "dex-config",
            "dex",
            data={"config.yaml": dump({"staticClients": [{"id": "bundler"}]})},
        )
    )
    with pytest.raises(StackError, match="bundler client credential; refusing rotation"):
        old_auth.credentials("auth")


def test_tracked_missing_bundler_key_requires_restore(old_auth):
    old_auth.state["credentials"]["bundler"] = {"component": "auth"}
    with pytest.raises(StackError, match="Restore missing generated credential: bundler"):
        old_auth.credentials("auth")


def test_bundler_dex_credential_mismatch_is_rejected(old_auth):
    live = old_auth.runner.get("secret", "dex-secrets", "dex")
    live["data"]["BUNDLER_CLIENT_SECRET"] = b64encode(b"existing-key").decode()
    old_auth.runner.put(live)
    old_auth.config.secrets.generated["bundler"] = "different-key"
    with pytest.raises(StackError, match="Credential mismatch.*BUNDLER_CLIENT_SECRET"):
        old_auth.credentials("auth")


def test_failed_bundler_policy_blocks_helm_upgrade_and_tracks_resources(
    addon, dex_credentials, monkeypatch
):
    from enkictl.postrender import LABEL

    values, password = addon._credentials()
    cluster = Cluster()
    addon.installation.runner = cluster
    addon.runner = cluster
    addon.installation.versions = {
        "v1",
        "apps/v1",
        "networking.k8s.io/v1",
        "gateway.envoyproxy.io/v1alpha1",
        "gateway.networking.k8s.io/v1",
    }
    monkeypatch.setattr(addon.installation, "bind", lambda **_: None)
    monkeypatch.setattr(
        addon.installation, "helm_install", lambda *_: pytest.fail("unsafe upgrade")
    )
    monkeypatch.setattr(addon, "_restore_uploads", lambda: None)
    monkeypatch.setattr(addon, "_ensure_recreate_strategy", lambda: None)

    def wait(docs):
        if any(doc["kind"] == "SecurityPolicy" for doc in docs):
            raise StackError("Bundler policy was rejected")

    monkeypatch.setattr(addon.installation, "wait_resources", wait)
    with pytest.raises(StackError, match="Bundler policy was rejected"):
        addon.install(values, password)
    assert addon.installation.state["addons"]["librechat"]["status"] == "failed"
    bundled = [
        record
        for record in addon.installation.state["resources"]
        if record["name"].startswith("librechat-bundler")
    ]
    assert {record["kind"] for record in bundled} == {
        "Secret",
        "ConfigMap",
        "Deployment",
        "Service",
        "NetworkPolicy",
        "Backend",
        "SecurityPolicy",
        "HTTPRoute",
    }
    assert all(record["component"] == OWNER for record in bundled)
    live = cluster.get("deployment", "librechat-bundler", addon.namespace)
    assert live["metadata"]["labels"][LABEL] == addon.installation.state["id"]
