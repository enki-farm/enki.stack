from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from enkictl.config import StackError
from enkictl.main import app
from enkictl.render import PHASES


@pytest.mark.parametrize("secret", [False, True])
@pytest.mark.parametrize("default", ["", "existing-value"])
def test_interactive_prompt_preserves_default(monkeypatch, secret, default):
    from enkictl import main

    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        main.questionary.Question, "ask", lambda self: self.application.current_buffer.text
    )

    assert main.prompt("Value", default, secret) == default


@pytest.mark.parametrize("secret", [False, True])
def test_interactive_prompt_cancellation_aborts(monkeypatch, secret):
    from enkictl import main

    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(main.questionary.Question, "ask", lambda self: None)

    with pytest.raises(typer.Abort):
        main.prompt("Value", secret=secret)


def test_install_dry_run_never_calls_mutation(config, monkeypatch):
    from enkictl import main

    mutations = []
    deployment = SimpleNamespace(
        profile=config.profile,
        resources={},
        releases=[],
        requested=["auth"],
        components=["auth"],
        acting=["auth"],
        phase_components=dict.fromkeys(PHASES, "auth"),
    )
    deployment.resources = dict.fromkeys(PHASES, [])
    installation = SimpleNamespace(install=lambda: mutations.append("install"))
    monkeypatch.setattr(main, "checked", lambda *_: (deployment, installation))
    result = CliRunner().invoke(app, ["component", "install", "--dry-run", "-p", str(config.path)])
    assert result.exit_code == 0, result.output
    assert "no cluster changes" in result.output
    assert mutations == []


def test_component_install_uses_inventory_selection(config, monkeypatch):
    from enkictl import main

    deployment = SimpleNamespace(
        profile=config.profile,
        resources=dict.fromkeys(PHASES, []),
        releases=[],
        requested=["auth"],
        components=["certificates", "ai-gateway", "auth"],
        acting=["auth"],
        phase_components=dict.fromkeys(PHASES, "auth"),
    )
    installation = SimpleNamespace(install=lambda: None)
    selected = SimpleNamespace(state={"desired": {"components": ["auth"]}})
    seen = []
    monkeypatch.setattr(main, "local_inventory", lambda *_: selected)
    monkeypatch.setattr(
        main,
        "checked",
        lambda _profile, _root, targets: seen.append(targets) or (deployment, installation),
    )

    result = CliRunner().invoke(app, ["component", "install", "--dry-run", "-p", str(config.path)])

    assert result.exit_code == 0, result.output
    assert seen == [["auth"]]


@pytest.mark.parametrize(
    "status", ["new", "installing", "installed", "failed", "uninstalling", "uninstalled", "purged"]
)
def test_component_and_addon_lists_show_recorded_status(profile_path, monkeypatch, status):
    from enkictl import main

    inventory = SimpleNamespace(
        state={
            "components": {
                "auth": {"status": "installed"},
                "kserve": {"status": status},
            },
            "addons": {
                "librechat": {"status": "installed"},
                "other-addon": {"status": status},
            },
        }
    )
    monkeypatch.setattr(main, "Installation", lambda _deployment: inventory)

    component_result = CliRunner().invoke(
        app,
        ["component", "list", "-p", str(profile_path)],
    )
    addon_result = CliRunner().invoke(app, ["addon", "list", "-p", str(profile_path)])

    assert component_result.exit_code == 0, component_result.output
    assert "auth: installed" in component_result.output
    assert f"kserve: {status}" in component_result.output
    assert addon_result.exit_code == 0, addon_result.output
    assert "librechat: installed" in addon_result.output
    assert f"other-addon: {status}" in addon_result.output


def test_component_group_lists_lifecycle_commands():
    result = CliRunner().invoke(app, ["component", "--help"])
    root_help = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert all(command in result.output for command in ("install", "uninstall", "list"))
    assert root_help.exit_code == 0, root_help.output
    assert "component" in root_help.output
    for command in ("install", "uninstall"):
        help_result = CliRunner().invoke(app, [command, "--help"])
        assert help_result.exit_code == 0, help_result.output
    removed = CliRunner().invoke(app, ["components", "--help"])
    assert removed.exit_code == 2, removed.output


def test_preflight_failure_prevents_install_and_confirmation(profile_path, monkeypatch):
    from enkictl import main

    def fail(*_):
        raise StackError("Known prerequisite failure")

    monkeypatch.setattr(main, "checked", fail)
    result = CliRunner().invoke(app, ["component", "install", "--yes", "-p", str(profile_path)])
    assert result.exit_code == 1
    assert "Known prerequisite failure" in result.output
    assert "Install this platform" not in result.output


def test_preflight_and_conflict_check_order(monkeypatch):
    from enkictl import main

    calls = []
    deployment = SimpleNamespace(
        profile=SimpleNamespace(context="test-context", platform="aks", domain="example.test"),
        resources={},
        releases=[],
    )
    installation = SimpleNamespace(conflicts=lambda: calls.append("conflicts"))
    monkeypatch.setattr(main, "prepare", lambda *_, **__: (deployment, installation))
    monkeypatch.setattr(
        main,
        "check_prerequisites",
        lambda _, progress: calls.append("preflight") or [],
    )
    main.checked(None, None)
    assert calls == ["preflight", "conflicts"]


def test_preflight_reports_progress_and_results(monkeypatch):
    from enkictl import main

    deployment = SimpleNamespace(
        profile=SimpleNamespace(context="test-context", platform="aks", domain="example.test"),
        resources={"base": [{"kind": "ConfigMap"}]},
        releases=[SimpleNamespace(name="test-release")],
        requested=[],
        components=[],
        acting=[],
        phase_components=dict.fromkeys(PHASES, "auth"),
    )
    installation = SimpleNamespace(conflicts=lambda: None)
    monkeypatch.setattr(main, "prepare", lambda *_, **__: (deployment, installation))

    def check(_, progress):
        progress("Versions passed: Helm v4.3.1.")
        return []

    monkeypatch.setattr(main, "check_prerequisites", check)
    result = CliRunner().invoke(app, ["preflight"])
    assert result.exit_code == 0, result.output
    assert "Target: test-context (aks, example.test)" in result.output
    assert "local composition passed (1 resources, 1 Helm releases)" in result.output
    assert "Versions passed: Helm v4.3.1." in result.output
    assert "Resource conflict check passed." in result.output
    assert "Preflight passed" in result.output


@pytest.fixture
def profile_inventory(profile_path):
    from enkictl import main

    inventory = main.local_inventory(profile_path, None)
    inventory.state["desired"] = {
        "components": ["kserve", "auth"],
        "addons": ["librechat"],
    }
    inventory.save()
    return inventory


@pytest.mark.parametrize("dry_run", [False, True])
def test_profile_install_orders_components_before_addons(profile_inventory, monkeypatch, dry_run):
    from enkictl import main

    calls = []

    def checked(profile, root, targets):
        assert targets == ["kserve", "auth"]
        assert root == profile_inventory.deployment.root
        calls.append("check-components")
        return SimpleNamespace(), profile_inventory

    def apply(_deployment, installation):
        calls.append("install-components")
        for name in ("certificates", "ai-gateway", "kserve", "auth"):
            installation.state["components"][name] = {"status": "installed"}
        installation.save()

    def addon_install(**kwargs):
        assert kwargs["yes"] is True
        assert kwargs["dry_run"] is dry_run
        current = main.local_inventory(kwargs["profile"], kwargs["root"])
        assert current.state["components"]["kserve"]["status"] == "installed"
        calls.append("install-addon")

    class Addon:
        dependencies = ("ai-gateway", "kserve")

        def __init__(self, **_kwargs):
            self.deployment = SimpleNamespace(render_charts=lambda: calls.append("render-addon"))

    monkeypatch.setattr(main, "ADDONS", {"librechat": Addon})
    monkeypatch.setattr(main, "checked", checked)
    monkeypatch.setattr(main, "preview", lambda _: None)
    monkeypatch.setattr(main, "apply_components", apply)
    monkeypatch.setattr(main, "install_addon", addon_install)
    args = [
        "install",
        "-p",
        str(profile_inventory.config.path),
        "--stack-root",
        str(profile_inventory.deployment.root),
        "--yes",
    ]
    if dry_run:
        args.append("--dry-run")
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    if dry_run:
        assert calls == ["check-components", "render-addon"]
        assert "Live preflight deferred" in result.output
        assert "NOT been verified" in result.output
        assert main.local_inventory(profile_inventory.config.path, None).state["components"] == {}
    else:
        assert calls == ["check-components", "install-components", "install-addon"]


def test_profile_addon_only_install_skips_core_and_runs_live_dry_run(
    profile_inventory, monkeypatch
):
    from enkictl import main

    profile_inventory.state["desired"]["components"] = []
    profile_inventory.state["components"] = {
        name: {"status": "installed"} for name in ("ai-gateway", "kserve", "auth")
    }
    profile_inventory.save()
    monkeypatch.setattr(main, "checked", lambda *_: pytest.fail("no core selected"))
    calls = []
    monkeypatch.setattr(main, "install_addon", lambda **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(
        app, ["install", "-p", str(profile_inventory.config.path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["name"] == "librechat"
    assert calls[0]["dry_run"] is True


@pytest.mark.parametrize(
    ("components", "addons", "error"),
    [
        ([], [], "Nothing selected"),
        (["gpu-operator"], ["librechat"], "requires components"),
        (["unknown"], [], "Unknown component"),
        (["gpu-operator"], ["unknown"], "Unknown addon"),
    ],
)
def test_profile_install_rejects_invalid_selection_before_changes(
    profile_inventory, monkeypatch, components, addons, error
):
    from enkictl import main

    profile_inventory.state["desired"] = {"components": components, "addons": addons}
    profile_inventory.save()
    monkeypatch.setattr(main, "checked", lambda *_: pytest.fail("invalid selection"))
    result = CliRunner().invoke(app, ["install", "-p", str(profile_inventory.config.path), "--yes"])
    assert result.exit_code == 1, result.output
    assert error in result.output


def test_profile_install_without_selection_uses_core_defaults(profile_inventory, monkeypatch):
    from enkictl import main

    profile_inventory.state["desired"] = {}
    profile_inventory.save()
    selected = []
    monkeypatch.setattr(
        main,
        "checked",
        lambda _profile, _root, targets: selected.extend(targets) or (None, None),
    )
    monkeypatch.setattr(main, "preview", lambda _: None)
    result = CliRunner().invoke(
        app, ["install", "-p", str(profile_inventory.config.path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert selected == main.defaults(profile_inventory.config.profile.gpu)


@pytest.mark.parametrize("failure_stage", ["check", "install"])
def test_profile_component_failure_stops_addons(profile_inventory, monkeypatch, failure_stage):
    from enkictl import main

    def fail(*_args):
        raise StackError("component failure")

    monkeypatch.setattr(
        main,
        "checked",
        fail if failure_stage == "check" else lambda *_: (None, profile_inventory),
    )
    monkeypatch.setattr(main, "preview", lambda _: None)
    monkeypatch.setattr(main, "apply_components", fail)
    monkeypatch.setattr(main, "install_addon", lambda **_: pytest.fail("components failed"))
    result = CliRunner().invoke(app, ["install", "-p", str(profile_inventory.config.path), "--yes"])
    assert result.exit_code == 1, result.output
    assert "component failure" in result.output


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("purge", [False, True])
def test_profile_uninstall_uses_recorded_items_and_orders_addons_first(
    profile_inventory, monkeypatch, dry_run, purge
):
    from enkictl import main

    profile_inventory.state["desired"] = {"components": [], "addons": []}
    profile_inventory.state["components"] = {
        "auth": {"status": "failed"},
        "kserve": {"status": "installed"},
        "monitoring": {"status": "uninstalled"},
        "gpu-operator": {"status": "purged"},
    }
    profile_inventory.state["addons"] = {"librechat": {"status": "failed"}}
    profile_inventory.save()
    calls = []
    monkeypatch.setattr(main, "uninstall_addon", lambda **kwargs: calls.append(("addon", kwargs)))
    monkeypatch.setattr(main, "uninstall", lambda **kwargs: calls.append(("components", kwargs)))
    args = ["uninstall", "-p", str(profile_inventory.config.path), "--yes"]
    if dry_run:
        args.append("--dry-run")
    if purge:
        args.extend(["--purge", "--confirm-data-loss"])
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert [name for name, _ in calls] == (
        ["addon", "components"] if dry_run else ["addon", "components", "addon", "components"]
    )
    for name, kwargs in calls:
        assert kwargs["purge"] is purge
        if name == "components":
            assert kwargs["targets"] == (
                ["auth", "kserve", "monitoring"] if purge else ["auth", "kserve"]
            )
    assert all(kwargs["dry_run"] for _, kwargs in calls[:2])
    assert all(not kwargs["dry_run"] for _, kwargs in calls[2:])


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_profile_operations_honor_declined_confirmation(profile_inventory, monkeypatch, command):
    from enkictl import main

    monkeypatch.setattr(main, "checked", lambda *_: (None, profile_inventory))
    monkeypatch.setattr(main, "preview", lambda _: None)
    monkeypatch.setattr(main, "apply_components", lambda *_: pytest.fail("declined"))
    monkeypatch.setattr(main, "install_addon", lambda **_: pytest.fail("declined"))
    profile_inventory.state["components"] = {"kserve": {"status": "installed"}}
    profile_inventory.save()
    calls = []
    monkeypatch.setattr(main, "uninstall", lambda **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(
        app, [command, "-p", str(profile_inventory.config.path)], input="n\n"
    )
    assert result.exit_code == 1, result.output
    assert all(kwargs["dry_run"] for kwargs in calls)


def test_profile_purge_confirms_data_loss_before_any_deletion(profile_inventory, monkeypatch):
    from enkictl import main

    profile_inventory.state["components"] = {"kserve": {"status": "installed"}}
    profile_inventory.state["addons"] = {"librechat": {"status": "uninstalled"}}
    profile_inventory.save()
    calls = []
    monkeypatch.setattr(main, "uninstall", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(main, "uninstall_addon", lambda **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(
        app,
        ["uninstall", "-p", str(profile_inventory.config.path), "--purge", "--yes"],
        input="n\n",
    )
    assert result.exit_code == 1, result.output
    assert "Confirm permanent managed data loss" in result.output
    assert len(calls) == 2
    assert all(kwargs["dry_run"] for kwargs in calls)


def test_profile_uninstall_addon_failure_stops_component_removal(profile_inventory, monkeypatch):
    from enkictl import main

    profile_inventory.state["components"] = {"kserve": {"status": "installed"}}
    profile_inventory.state["addons"] = {"librechat": {"status": "installed"}}
    profile_inventory.save()

    def addon(**kwargs):
        if not kwargs["dry_run"]:
            raise StackError("addon removal failed")

    def components(**kwargs):
        assert kwargs["dry_run"], "must not remove components after addon failure"

    monkeypatch.setattr(main, "uninstall_addon", addon)
    monkeypatch.setattr(main, "uninstall", components)
    result = CliRunner().invoke(
        app, ["uninstall", "-p", str(profile_inventory.config.path), "--yes"]
    )
    assert result.exit_code == 1, result.output
    assert "addon removal failed" in result.output


def test_profile_uninstall_requires_existing_inventory(profile_path):
    result = CliRunner().invoke(app, ["uninstall", "-p", str(profile_path), "--yes"])
    assert result.exit_code == 1, result.output
    assert "No installation inventory" in result.output


def test_profile_uninstall_skips_already_removed_items(profile_inventory, monkeypatch):
    from enkictl import main

    profile_inventory.state["components"] = {"kserve": {"status": "uninstalled"}}
    profile_inventory.state["addons"] = {"librechat": {"status": "uninstalled"}}
    profile_inventory.save()
    monkeypatch.setattr(main, "uninstall", lambda **_: pytest.fail("already removed"))
    monkeypatch.setattr(main, "uninstall_addon", lambda **_: pytest.fail("already removed"))
    result = CliRunner().invoke(
        app, ["uninstall", "-p", str(profile_inventory.config.path), "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "No recorded components or addons require removal" in result.output


def test_profile_uninstall_rejects_unknown_addon_before_removing_components(
    profile_inventory, monkeypatch
):
    from enkictl import main

    profile_inventory.state["components"] = {"kserve": {"status": "installed"}}
    profile_inventory.state["addons"] = {"unknown": {"status": "failed"}}
    profile_inventory.save()
    monkeypatch.setattr(main, "uninstall", lambda **_: pytest.fail("unknown addon"))
    result = CliRunner().invoke(
        app, ["uninstall", "-p", str(profile_inventory.config.path), "--yes"]
    )
    assert result.exit_code == 1, result.output
    assert "Unknown recorded addon" in result.output


def test_profile_uninstall_orders_dependents_before_dependencies(profile_inventory, monkeypatch):
    from enkictl import main

    profile_inventory.state["components"] = {}
    profile_inventory.state["addons"] = {
        "librechat": {"status": "installed"},
        "crawl4ai": {"status": "installed"},
        "searxng": {"status": "installed"},
    }
    profile_inventory.save()
    calls = []
    monkeypatch.setattr(main, "uninstall_addon", lambda **kwargs: calls.append(kwargs["name"]))
    result = CliRunner().invoke(
        app, ["uninstall", "-p", str(profile_inventory.config.path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert calls == ["librechat", "crawl4ai", "searxng"]


def test_profile_addon_failure_retains_completed_component_inventory(
    profile_inventory, monkeypatch
):
    from enkictl import main

    monkeypatch.setattr(main, "checked", lambda *_: (None, profile_inventory))
    monkeypatch.setattr(main, "preview", lambda _: None)

    def apply(_deployment, installation):
        installation.state["components"]["kserve"] = {"status": "installed"}
        installation.save()

    def addon(**_kwargs):
        raise StackError("external addon prerequisite missing")

    monkeypatch.setattr(main, "apply_components", apply)
    monkeypatch.setattr(main, "install_addon", addon)
    result = CliRunner().invoke(app, ["install", "-p", str(profile_inventory.config.path), "--yes"])
    assert result.exit_code == 1, result.output
    assert "external addon prerequisite missing" in result.output
    current = main.local_inventory(profile_inventory.config.path, None)
    assert current.state["components"]["kserve"]["status"] == "installed"
    assert current.state["desired"] == profile_inventory.state["desired"]


def test_profile_uninstall_reloads_inventory_between_addon_and_component_removal(
    profile_inventory, monkeypatch
):
    from enkictl import main

    profile_inventory.state["components"] = {"kserve": {"status": "installed"}}
    profile_inventory.state["addons"] = {"librechat": {"status": "installed"}}
    profile_inventory.save()
    monkeypatch.setattr(main.Installation, "bind", lambda *_args, **_kwargs: None)

    def remove_addon(installation, name, purge):
        assert not purge
        installation.state["addons"][name]["status"] = "uninstalled"
        installation.save()
        return []

    def remove_components(installation, purge, targets):
        assert not purge
        assert targets == ["kserve"]
        assert installation.state["addons"]["librechat"]["status"] == "uninstalled"
        installation.state["components"]["kserve"]["status"] = "uninstalled"
        installation.save()
        return []

    monkeypatch.setattr(main.Installation, "uninstall_addon", remove_addon)
    monkeypatch.setattr(main.Installation, "uninstall", remove_components)
    result = CliRunner().invoke(
        app, ["uninstall", "-p", str(profile_inventory.config.path), "--yes"]
    )
    assert result.exit_code == 0, result.output
    current = main.local_inventory(profile_inventory.config.path, None)
    assert current.state["components"]["kserve"]["status"] == "uninstalled"
    assert current.state["addons"]["librechat"]["status"] == "uninstalled"
