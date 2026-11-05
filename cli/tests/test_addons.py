import io
import json
import tarfile
from base64 import b64decode, b64encode, urlsafe_b64decode
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_private_key,
    load_der_public_key,
    load_pem_private_key,
)
from typer.testing import CliRunner

from enkictl.addons.librechat import (
    CHAT_SECRET,
    CODEAPI_IMAGE_REGISTRY,
    CODEAPI_IMAGE_TAG,
    CODEAPI_IMAGES,
    CODEAPI_RELEASE,
    CODEAPI_SECRET,
    CODEAPI_SECRET_KEYS,
    NAME,
    OWNER,
    LibreChatAddon,
    _replace_domain,
    _validate_archive,
)
from enkictl.config import StackError, read_yaml
from enkictl.main import app
from enkictl.render import resource

ROOT = Path(__file__).resolve().parents[2]


def test_domain_replacement_is_recursive():
    assert _replace_domain(
        {"host": "https://chat.zer0.garden", "items": ["ai.zer0.garden"]}, "platform.test"
    ) == {"host": "https://chat.platform.test", "items": ["ai.platform.test"]}


def test_librechat_composition_uses_profile_domain(profile_path, monkeypatch):
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    addon = LibreChatAddon(profile_path, ROOT, with_code_interpreter=True)

    values = addon.deployment.overrides["librechat"]
    assert values["librechat"]["configEnv"]["DOMAIN_CLIENT"] == "https://chat.platform.internal"
    assert values["librechat"]["configEnv"]["DOMAIN_SERVER"] == "https://chat.platform.internal"
    assert values["librechat"]["configEnv"]["RAG_API_URL"] == "http://librechat-rag-api:8000"
    assert values["librechat"]["configEnv"]["LIBRECHAT_CODE_BASEURL"] == (
        "http://codeapi-api.librechat.svc.cluster.local:3112"
    )
    assert values["librechat"]["configEnv"]["CODEAPI_AUTH_PROVIDER"] == "librechat-jwt"
    config = yaml.safe_load(values["librechat"]["configYamlContent"])
    assert config["interface"]["runCode"] is True
    assert [release.name for release in addon.deployment.releases] == [CODEAPI_RELEASE, "librechat"]
    codeapi_values = addon.deployment.overrides[CODEAPI_RELEASE]
    image_values = [
        codeapi_values["api"]["image"],
        codeapi_values["workerSandbox"]["workerImage"],
        codeapi_values["workerSandbox"]["sandboxImage"],
        codeapi_values["workerSandbox"]["packages"]["initJob"]["image"],
        codeapi_values["fileServer"]["image"],
        codeapi_values["egressGateway"]["image"],
    ]
    assert {image["repository"].rsplit("/", 1)[-1] for image in image_values} == set(
        CODEAPI_IMAGES
    )
    assert all(
        image["repository"].startswith(f"{CODEAPI_IMAGE_REGISTRY}/")
        and image["tag"] == CODEAPI_IMAGE_TAG
        for image in image_values
    )
    seccomp_check = next(
        doc
        for doc in addon.deployment.resources["content"]
        if doc["kind"] == "DaemonSet" and doc["metadata"]["name"] == "codeapi-seccomp-check"
    )
    assert seccomp_check["spec"]["progressDeadlineSeconds"] == 60
    assert "programmatic_tools" not in config.get("endpoints", {}).get("agents", {}).get(
        "capabilities", []
    )
    assert "https://ai.platform.internal/v1" in values["librechat"]["configYamlContent"]
    chat_route = next(
        doc for doc in addon.deployment.resources["content"] if doc["kind"] == "HTTPRoute"
    )
    assert chat_route["spec"]["hostnames"] == ["chat.platform.internal"]


def test_librechat_rejects_unsupported_platform(profile_path, monkeypatch):
    profile_path.write_text(
        profile_path.read_text().replace("dgx-spark", "aks").replace("operator", "existing")
    )
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    with pytest.raises(StackError, match="only dgx-spark"):
        LibreChatAddon(profile_path, ROOT)


def test_codeapi_image_preflight_requires_arm64(profile_path, monkeypatch):
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    addon = LibreChatAddon(profile_path, ROOT)
    inspected = []

    def inspect(args):
        inspected.append(args[-1])
        return "Platform: linux/arm64" if len(inspected) < len(CODEAPI_IMAGES) else "linux/amd64"

    monkeypatch.setattr(addon.runner, "run", inspect)
    with pytest.raises(StackError, match="not available for linux/arm64"):
        addon._check_codeapi_images()
    assert len(inspected) == len(CODEAPI_IMAGES)


def test_codeapi_image_preflight_reports_build_command(profile_path, monkeypatch):
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    addon = LibreChatAddon(profile_path, ROOT)
    monkeypatch.setattr(
        addon.runner,
        "run",
        lambda *_: (_ for _ in ()).throw(StackError("image not found")),
    )
    with pytest.raises(
        StackError, match="log in to GHCR and run bash scripts/build-code-interpreter-images.sh"
    ):
        addon._check_codeapi_images()


def test_addon_credentials_reuse_crawl4ai_secret_and_generate_persistable_values(
    profile_path, monkeypatch
):
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    addon = LibreChatAddon(profile_path, ROOT, with_code_interpreter=True)
    crawl_secret = {
        "data": {
            "FIRECRAWL_SHIM_API_KEY": b64encode(b"crawl-shim").decode(),
            "CRAWL4AI_API_TOKEN": b64encode(b"crawl-token").decode(),
        }
    }

    def get(_runner, kind, name, namespace=""):
        if kind == "secret" and namespace == "crawl4ai" and name == "crawl4ai-credentials":
            return crawl_secret
        if kind == "secret" and namespace == "dex" and name == "dex-secrets":
            return {
                "data": {
                    "LIBRECHAT_CLIENT_SECRET": b64encode(b"oidc-client").decode(),
                    "BUNDLER_CLIENT_SECRET": b64encode(b"bundler-client").decode(),
                }
            }
        return None

    monkeypatch.setattr("enkictl.addons.librechat.Runner.get", get)
    values, database_password = addon._credentials()

    assert values["FIRECRAWL_API_KEY"] == "crawl-shim"
    assert values["CRAWL4AI_API_TOKEN"] == "crawl-token"
    assert values["ENKI_GATEWAY_API_KEY"] == "sk-local-vllm"
    assert len(values["CREDS_IV"]) == 32
    assert len(values["CREDS_KEY"]) == 64
    assert len(database_password) == 64
    assert values["OPENID_CLIENT_SECRET"] == "oidc-client"
    assert len(values["OPENID_SESSION_SECRET"]) == 64
    codeapi_key = load_pem_private_key(values["CODEAPI_JWT_PRIVATE_KEY"].encode(), password=None)
    assert isinstance(codeapi_key, Ed25519PrivateKey)
    jwks = json.loads(values["CODEAPI_JWT_JWKS_JSON"])
    assert jwks["keys"][0]["kid"] == "enki-codeapi-ed25519-1"
    jwks_public = urlsafe_b64decode(jwks["keys"][0]["x"] + "==")
    assert jwks_public == codeapi_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    manifest_private = load_der_private_key(
        b64decode(values["CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY"]), password=None
    )
    manifest_public = load_der_public_key(
        b64decode(values["CODEAPI_EXECUTION_MANIFEST_PUBLIC_KEY"])
    )
    assert manifest_private.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    ) == manifest_public.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    codeapi_values = addon.deployment.overrides[CODEAPI_RELEASE]
    assert codeapi_values["workerSandbox"]["kvmEnabled"] is False
    assert codeapi_values["workerSandbox"]["seccomp"]["enabled"] is True
    assert codeapi_values["workerSandbox"]["sandbox"]["disableNetworking"] is True
    assert codeapi_values["toolCallServer"]["enabled"] is False
    assert codeapi_values["redis"]["auth"]["password"] == values["CODEAPI_REDIS_PASSWORD"]
    assert codeapi_values["minio"]["auth"]["rootPassword"] == values[
        "CODEAPI_MINIO_ROOT_PASSWORD"
    ]
    assert codeapi_values["executionManifest"]["privateKey"] == values[
        "CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY"
    ]
    persisted_key = values["CODEAPI_JWT_PRIVATE_KEY"]

    def get_with_chat_key(_runner, kind, name, namespace=""):
        if kind == "secret" and name == CHAT_SECRET:
            return {"data": {"CODEAPI_JWT_PRIVATE_KEY": b64encode(persisted_key.encode()).decode()}}
        if kind == "secret" and name == CODEAPI_SECRET:
            return {
                "data": {
                    key: b64encode(values[key].encode()).decode() for key in CODEAPI_SECRET_KEYS
                }
            }
        return get(_runner, kind, name, namespace)

    monkeypatch.setattr("enkictl.addons.librechat.Runner.get", get_with_chat_key)
    reused_values, _ = addon._credentials()
    assert reused_values["CODEAPI_JWT_PRIVATE_KEY"] == values["CODEAPI_JWT_PRIVATE_KEY"]
    for key in CODEAPI_SECRET_KEYS:
        assert reused_values[key] == values[key]
    assert addon.config.secrets.addons[NAME] == {}


def test_addon_preflight_uses_general_checks_without_treating_addon_as_component(
    profile_path, monkeypatch
):
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    addon = LibreChatAddon(profile_path, ROOT)
    addon.preflight = lambda: ({"ENKI_GATEWAY_API_KEY": "key"}, "database")
    monkeypatch.setattr(addon, "_check_upload_migration", lambda: None)
    seen = []

    def check(deployment, progress=None):
        seen.extend(deployment.acting)
        progress("preflight passed")
        return ["review DNS"]

    monkeypatch.setattr("enkictl.addons.librechat.check_prerequisites", check)
    monkeypatch.setattr(addon.installation, "conflicts", lambda: None)
    values, password, warnings = addon.check(lambda message: seen.append(message))

    assert values == {"ENKI_GATEWAY_API_KEY": "key"}
    assert password == "database"
    assert warnings[0] == "review DNS"
    assert "Artifacts require DNS" in warnings[1]
    assert seen == [OWNER, "preflight passed"]
    assert addon.deployment.acting == []


def test_upload_migration_guard_accepts_managed_mount_and_rejects_unarchived_files(
    profile_path, monkeypatch
):
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    addon = LibreChatAddon(profile_path, ROOT)
    existing = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"volumeMounts": [{"name": "uploads", "mountPath": "/app/uploads"}]}
                    ],
                    "volumes": [
                        {
                            "name": "uploads",
                            "persistentVolumeClaim": {"claimName": "librechat-uploads"},
                        }
                    ],
                }
            }
        }
    }
    monkeypatch.setattr(
        "enkictl.addons.librechat.Runner.get",
        lambda _runner, *_args, **_kwargs: existing,
    )
    monkeypatch.setattr(
        "enkictl.addons.librechat.Runner.kubectl",
        lambda *_args, **_kwargs: pytest.fail("managed uploads should not be inspected"),
    )
    addon._check_upload_migration()

    existing["spec"]["template"]["spec"]["volumes"] = []
    monkeypatch.setattr("enkictl.addons.librechat.Runner.kubectl", lambda *_args, **_kwargs: "3\n")
    with pytest.raises(StackError, match="Existing uploads need migration"):
        addon._check_upload_migration()


@pytest.mark.parametrize("dry_run", [False, True])
def test_install_command_tracks_resources_and_persists_credentials(
    profile_path, monkeypatch, dry_run
):
    from enkictl import main

    rag = (
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: librechat-rag-api\n"
        "---\napiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: librechat-rag-db\n"
        "---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: librechat-rag-api\n"
        "spec:\n  template:\n    spec:\n      containers:\n        - name: rag-api\n"
        "          image: ignored\n"
    )
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: rag)
    addon = LibreChatAddon(profile_path, ROOT)
    addon.deployment.resources["credentials"] = [
        resource(
            "Secret",
            "librechat-bundler-oidc",
            addon.namespace,
            data={"client-secret": b64encode(b"bundler-client").decode()},
        )
    ]
    addon.installation.state["components"]["kserve"] = {"status": "installed"}
    addon.installation.state["desired"] = {"components": [], "addons": []}
    addon.installation.save()
    before = addon.installation.path.read_text()
    addon.deployment.templates["librechat"] = []
    values = {"ENKI_GATEWAY_API_KEY": "test-key"}
    monkeypatch.setitem(main.ADDONS, NAME, lambda **_: addon)
    monkeypatch.setattr(addon, "check", lambda **_: (values, "test-db-password", []))
    calls = []
    monkeypatch.setattr(addon.installation, "bind", lambda **_kwargs: calls.append("bind"))
    monkeypatch.setattr(
        addon.installation,
        "apply",
        lambda doc, _phase: calls.append(f"apply:{doc['kind']}:{doc['metadata']['name']}"),
    )
    monkeypatch.setattr(
        addon.installation, "wait_resources", lambda docs: calls.append(f"wait:{len(docs)}")
    )
    monkeypatch.setattr(addon.installation, "helm_install", lambda _release: calls.append("helm"))
    monkeypatch.setattr(addon.installation, "capture_generated", lambda: None)
    monkeypatch.setattr(
        addon.runner,
        "get",
        lambda kind, _name, _namespace="": (
            {"spec": {"strategy": {"type": "RollingUpdate"}}} if kind == "deployment" else None
        ),
    )
    monkeypatch.setattr(
        addon.runner, "kubectl", lambda *args, **_kwargs: calls.append(f"kubectl:{args[0]}")
    )

    result = CliRunner().invoke(
        app,
        ["addon", "install", NAME, "-p", str(profile_path), "--yes"]
        + (["--dry-run"] if dry_run else []),
    )

    assert result.exit_code == 0, result.output
    saved = read_yaml(addon.installation.path)
    assert saved["components"] == {"kserve": {"status": "installed"}}
    assert saved["desired"] == {"components": [], "addons": []}
    assert main.local_inventory(profile_path, None).state == saved
    listed = CliRunner().invoke(app, ["addon", "list", "-p", str(profile_path)])
    assert listed.exit_code == 0, listed.output
    if dry_run:
        assert addon.installation.path.read_text() == before
        assert saved["addons"] == {}
        assert "  none" in listed.output
        assert calls == []
        return

    assert calls.index("kubectl:patch") < calls.index("helm")
    assert calls.index("apply:Secret:librechat-bundler-oidc") < calls.index(
        "apply:SecurityPolicy:librechat-bundler-auth"
    )
    assert (
        calls.index("apply:SecurityPolicy:librechat-bundler-auth")
        < calls.index("apply:HTTPRoute:librechat-bundler")
        < calls.index("helm")
    )
    assert calls.index("apply:Deployment:librechat-bundler") < calls.index("helm")
    assert calls.index("helm") < calls.index("apply:HTTPRoute:librechat")
    assert addon.installation.state["addons"][NAME]["status"] == "installed"
    assert saved["addons"][NAME]["status"] == "installed"
    assert f"  {NAME}: installed\n" in listed.output
    assert "test-db-password" in (profile_path.parent / "secrets.yaml").read_text()


def test_upload_archive_rejects_traversal_and_links(tmp_path):
    archive_path = tmp_path / "uploads.tar"
    with tarfile.open(archive_path, "w") as archive:
        contents = b"safe"
        file = tarfile.TarInfo("uploads/file.txt")
        file.size = len(contents)
        archive.addfile(file, io.BytesIO(contents))
    _validate_archive(archive_path)

    for member in ("../escape", "/absolute", "uploads/link"):
        with tarfile.open(archive_path, "w") as archive:
            if member.endswith("link"):
                item = tarfile.TarInfo(member)
                item.type = tarfile.SYMTYPE
                item.linkname = "../../outside"
                archive.addfile(item)
            else:
                item = tarfile.TarInfo(member)
                item.size = 0
                archive.addfile(item, io.BytesIO())
        with pytest.raises(StackError, match="unsafe"):
            _validate_archive(archive_path)


def test_unknown_addon_fails_before_profile_access():
    result = CliRunner().invoke(app, ["addon", "install", "typo", "--profile", "/not/a/profile"])
    assert result.exit_code == 1
    assert "Unknown addon: typo" in result.output


def test_addon_dry_run_never_installs(profile_path, monkeypatch):
    from enkictl import main

    calls = []

    class FakeAddon:
        def __init__(self, **_kwargs):
            self.config = type(
                "ProfileConfig",
                (),
                {"profile": type("Profile", (), {"context": "test", "domain": "example.test"})()},
            )()
            self.deployment = type(
                "Deployment",
                (),
                {
                    "templates": {"librechat": [{}]},
                    "resources": {"content": [{}]},
                },
            )()

        def check(self, progress=None):
            progress("preflight passed")
            return {"ENKI_GATEWAY_API_KEY": "test-key"}, "db-password", []

        def install(self, *_args):
            calls.append("install")

    monkeypatch.setattr(main, "ADDONS", {"librechat": FakeAddon})
    result = CliRunner().invoke(
        app,
        ["addon", "install", "librechat", "-p", str(profile_path), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "Dry run complete" in result.output
    assert calls == []


def test_addon_install_uses_inventory_selection(profile_path, monkeypatch):
    from enkictl import main

    calls = []

    class FakeAddon:
        def __init__(self, **_kwargs):
            self.config = type(
                "ProfileConfig",
                (),
                {"profile": type("Profile", (), {"context": "test", "domain": "example.test"})()},
            )()
            self.deployment = type(
                "Deployment",
                (),
                {
                    "templates": {"librechat": [{}]},
                    "resources": {"content": [{}]},
                },
            )()

        def check(self, progress=None):
            progress("preflight passed")
            return {"ENKI_GATEWAY_API_KEY": "real-key"}, "db-password", []

        def install(self, *_args):
            calls.append("install")

    monkeypatch.setattr(main, "ADDONS", {"librechat": FakeAddon})
    monkeypatch.setattr(
        main,
        "local_inventory",
        lambda *_: type("Inventory", (), {"state": {"desired": {"addons": ["librechat"]}}})(),
    )
    result = CliRunner().invoke(
        app,
        ["addon", "install", "-p", str(profile_path), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "addon librechat" in result.output
    assert "Dry run complete" in result.output
    assert calls == []


def test_addon_help_lists_list_install_and_uninstall():
    result = CliRunner().invoke(app, ["addon", "--help"])
    assert result.exit_code == 0
    assert all(command in result.output for command in ("list", "install", "uninstall"))
