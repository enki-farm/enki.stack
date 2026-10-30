from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from enkictl.addons.internal import Crawl4aiAddon, SearxngAddon
from enkictl.config import StackError, read_yaml
from enkictl.lifecycle import Installation
from enkictl.main import addon_order, app

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = """apiVersion: v1
kind: Namespace
metadata:
  name: crawl4ai
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: crawl4ai
  namespace: crawl4ai
spec:
  template:
    spec:
      containers:
        - name: crawl4ai
          env:
            - name: LLM_BASE_URL
              value: https://ai.zer0.garden/v1
"""


@pytest.fixture
def internal_addon(profile_path, monkeypatch):
    monkeypatch.setattr("enkictl.addons.internal.Runner.render", lambda *_: MANIFESTS)
    monkeypatch.setattr("enkictl.addons.internal.Runner.get", lambda *_: None)
    return Crawl4aiAddon(profile_path, ROOT)


def test_registry_resolves_search_and_crawl_before_librechat():
    assert addon_order(["librechat", "searxng"]) == ["searxng", "crawl4ai", "librechat"]


def test_addon_cycles_are_rejected(monkeypatch):
    from enkictl import main

    monkeypatch.setattr(main, "ADDONS", {"cycle": SimpleNamespace(addon_dependencies=("cycle",))})
    with pytest.raises(StackError, match="dependency cycle"):
        addon_order(["cycle"])


def test_crawl_configuration_uses_profile_domain(internal_addon):
    container = internal_addon.deployment.resources["content"][1]["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert container["env"][0]["value"] == "https://ai.platform.internal/v1"
    assert internal_addon.deployment.components == []
    assert internal_addon.namespace == "crawl4ai"


@pytest.mark.parametrize("addon_type", [SearxngAddon, Crawl4aiAddon])
def test_credentials_are_generated_without_persisting_on_check(
    internal_addon, profile_path, addon_type
):
    addon = addon_type(profile_path, ROOT)
    values = addon._credentials()
    assert set(values) == set(addon.secret_keys)
    for key, value in values.items():
        assert value == "sk-local-vllm" if key == "OPENAI_API_KEY" else len(value) == 64
    assert addon.config.secrets.addons == {}


def test_credentials_reuse_live_secret_and_reject_rotation(internal_addon, monkeypatch):
    values = {key: f"stored-{key}" for key in internal_addon.secret_keys}
    monkeypatch.setattr(
        internal_addon.runner,
        "get",
        lambda *_: {
            "data": {key: b64encode(value.encode()).decode() for key, value in values.items()}
        },
    )
    assert internal_addon._credentials() == values
    internal_addon.config.secrets.addons["crawl4ai"] = {"CRAWL4AI_API_TOKEN": "changed"}
    with pytest.raises(StackError, match="differs"):
        internal_addon._credentials()


def test_invalid_secret_is_not_silently_regenerated(internal_addon, monkeypatch):
    monkeypatch.setattr(
        internal_addon.runner, "get", lambda *_: {"data": {"CRAWL4AI_API_TOKEN": "%%%"}}
    )
    with pytest.raises(StackError, match="invalid encoded data"):
        internal_addon._credentials()


def test_crawl_requires_installed_gateway(internal_addon):
    with pytest.raises(StackError, match="installed ai-gateway"):
        internal_addon.check()


def prepare_check(addon, monkeypatch):
    addon.installation.state["components"]["ai-gateway"] = {"status": "installed"}
    monkeypatch.setattr(addon.installation, "verify_dependency", lambda *_: None)
    monkeypatch.setattr("enkictl.addons.internal.check_prerequisites", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(addon.installation, "conflicts", lambda: None)


def test_check_includes_secret_ownership_and_capacity(internal_addon, monkeypatch):
    prepare_check(internal_addon, monkeypatch)
    seen = []

    def prerequisites(deployment, progress=None):
        seen.append(deployment.acting[:])
        assert deployment.resources["credentials"][0]["metadata"]["name"] == "crawl4ai-credentials"
        return []

    monkeypatch.setattr("enkictl.addons.internal.check_prerequisites", prerequisites)
    monkeypatch.setattr(internal_addon.installation, "conflicts", lambda: seen.append("ownership"))
    values, _, warnings = internal_addon.check()
    assert seen == [["addon:crawl4ai"], "ownership"]
    assert internal_addon.deployment.acting == []
    assert values["OPENAI_API_KEY"] == "sk-local-vllm"
    assert any("placeholder" in warning for warning in warnings)
    assert internal_addon.config.secrets.addons == {}


@pytest.mark.parametrize("fail", [False, True])
def test_install_tracks_resources_credentials_and_failure(internal_addon, monkeypatch, fail):
    prepare_check(internal_addon, monkeypatch)
    values, password, _ = internal_addon.check()
    calls = []
    monkeypatch.setattr(internal_addon.installation, "bind", lambda **_: calls.append("bind"))

    def apply(doc, _phase):
        assert internal_addon.installation.component == "addon:crawl4ai"
        calls.append(doc["kind"])
        if fail and doc["kind"] == "Deployment":
            raise StackError("rollout failed")

    monkeypatch.setattr(internal_addon.installation, "apply", apply)
    monkeypatch.setattr(
        internal_addon.installation, "wait_resources", lambda _: calls.append("wait")
    )
    if fail:
        with pytest.raises(StackError, match="partial inventory retained.*rollout failed"):
            internal_addon.install(values, password)
    else:
        internal_addon.install(values, password)
        assert calls == ["bind", "Namespace", "Secret", "Deployment", "wait"]
    saved = read_yaml(internal_addon.installation.path)
    assert saved["addons"]["crawl4ai"]["status"] == ("failed" if fail else "installed")
    assert saved["components"] == {"ai-gateway": {"status": "installed"}}
    assert internal_addon.installation.component is None
    secrets = read_yaml(internal_addon.config.secret_path)
    assert secrets["addons"]["crawl4ai"] == values
    assert internal_addon.config.secret_path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("dependency_failure", [False, True])
def test_cli_installs_dependencies_before_dependent_checks(
    profile_path, monkeypatch, dry_run, dependency_failure
):
    from enkictl import main

    calls = []
    state = {"addons": {}}
    registry = {}
    for name in ("searxng", "crawl4ai", "librechat"):

        class FakeAddon:
            def __init__(self, _name=name, **_kwargs):
                self.name = self.namespace = _name
                self.config = SimpleNamespace(
                    profile=SimpleNamespace(context="test", domain="test.internal")
                )
                self.installation = SimpleNamespace(state=state, path="inventory.yaml")
                self.deployment = SimpleNamespace(
                    templates={},
                    resources={},
                    render_charts=lambda: calls.append(f"render:{self.name}"),
                )

            def check(self, **_kwargs):
                calls.append(f"check:{self.name}")
                if self.name == "librechat":
                    assert all(
                        state["addons"][name]["status"] == "installed"
                        for name in ("searxng", "crawl4ai")
                    )
                return {}, "", []

            def install(self, *_args):
                calls.append(f"install:{self.name}")
                if dependency_failure and self.name == "searxng":
                    raise StackError("dependency failure")
                state["addons"][self.name] = {"status": "installed"}

        registry[name] = FakeAddon
    registry["librechat"].addon_dependencies = ("searxng", "crawl4ai")
    monkeypatch.setattr(main, "ADDONS", registry)
    result = CliRunner().invoke(
        app,
        ["addon", "install", "librechat", "-p", str(profile_path), "--yes"]
        + (["--dry-run"] if dry_run else []),
    )
    if dry_run:
        assert result.exit_code == 0, result.output
        assert calls == ["check:searxng", "check:crawl4ai", "render:librechat"]
        assert "NOT been verified" in result.output
        assert state["addons"] == {}
    elif dependency_failure:
        assert result.exit_code == 1, result.output
        assert calls == ["check:searxng", "install:searxng"]
    else:
        assert result.exit_code == 0, result.output
        assert calls == [
            "check:searxng",
            "install:searxng",
            "check:crawl4ai",
            "install:crawl4ai",
            "check:librechat",
            "install:librechat",
        ]


def test_cli_declined_confirmation_never_installs_dependencies(profile_path, monkeypatch):
    from enkictl import main

    class FakeAddon:
        def __init__(self, **_kwargs):
            self.config = SimpleNamespace(profile=SimpleNamespace(context="test"))
            self.deployment = SimpleNamespace(templates={}, resources={})

        def check(self, **_kwargs):
            return {}, "", []

        def install(self, *_args):
            pytest.fail("declined confirmation must not install")

    monkeypatch.setattr(main, "ADDONS", {"searxng": FakeAddon})
    result = CliRunner().invoke(
        app, ["addon", "install", "searxng", "-p", str(profile_path)], input="n\n"
    )
    assert result.exit_code == 1, result.output
    assert "Aborted" in result.output


def test_librechat_options_rejected_for_internal_addons(profile_path):
    result = CliRunner().invoke(
        app, ["addon", "install", "searxng", "-p", str(profile_path), "--namespace", "other"]
    )
    assert result.exit_code == 1
    assert "apply only to LibreChat" in result.output


@pytest.mark.parametrize("name", ["searxng", "crawl4ai"])
def test_cannot_remove_active_librechat_dependencies(internal_addon, name):
    internal_addon.installation.state["addons"]["librechat"] = {"status": "installed"}
    with pytest.raises(StackError, match="uninstall librechat first"):
        internal_addon.installation.uninstall_addon(name)


def test_cli_dry_run_does_not_save_internal_secrets(internal_addon, monkeypatch):
    from enkictl import main

    prepare_check(internal_addon, monkeypatch)
    monkeypatch.setitem(main.ADDONS, "crawl4ai", lambda **_: internal_addon)
    before = internal_addon.config.secret_path.read_text()
    result = CliRunner().invoke(
        app, ["addon", "install", "crawl4ai", "-p", str(internal_addon.config.path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert internal_addon.config.secret_path.read_text() == before
    assert not internal_addon.installation.path.exists()
    assert not Installation(internal_addon.deployment).exists
