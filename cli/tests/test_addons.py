import io
import tarfile
from base64 import b64encode
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enkictl.addons.librechat import (
    NAME,
    OWNER,
    LibreChatAddon,
    _replace_domain,
    _validate_archive,
)
from enkictl.config import StackError, read_yaml
from enkictl.main import app

ROOT = Path(__file__).resolve().parents[2]


def test_domain_replacement_is_recursive():
    assert _replace_domain(
        {"host": "https://chat.zer0.garden", "items": ["ai.zer0.garden"]}, "platform.test"
    ) == {"host": "https://chat.platform.test", "items": ["ai.platform.test"]}


def test_librechat_composition_uses_profile_domain(profile_path, monkeypatch):
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    addon = LibreChatAddon(profile_path, ROOT)

    values = addon.deployment.overrides["librechat"]
    assert values["librechat"]["configEnv"]["DOMAIN_CLIENT"] == "https://chat.platform.internal"
    assert values["librechat"]["configEnv"]["DOMAIN_SERVER"] == "https://chat.platform.internal"
    assert values["librechat"]["configEnv"]["RAG_API_URL"] == "http://librechat-rag-api:8000"
    assert "https://ai.platform.internal/v1" in values["librechat"]["configYamlContent"]
    assert addon.deployment.resources["content"][-1]["spec"]["hostnames"] == [
        "chat.platform.internal"
    ]


def test_librechat_rejects_unsupported_platform(profile_path, monkeypatch):
    profile_path.write_text(
        profile_path.read_text().replace("dgx-spark", "aks").replace("operator", "existing")
    )
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    with pytest.raises(StackError, match="only dgx-spark"):
        LibreChatAddon(profile_path, ROOT)


def test_addon_credentials_reuse_crawl4ai_secret_and_generate_persistable_values(
    profile_path, monkeypatch
):
    monkeypatch.setattr("enkictl.addons.librechat.Runner.render", lambda *_: "")
    addon = LibreChatAddon(profile_path, ROOT)
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
            return {"data": {"LIBRECHAT_CLIENT_SECRET": b64encode(b"oidc-client").decode()}}
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
    assert warnings == ["review DNS"]
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
