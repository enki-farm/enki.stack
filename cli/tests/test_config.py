import os

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from enkictl.config import Configuration, Profile, StackError, dump, private_write, read_yaml
from enkictl.main import app


def test_valid_configuration(config):
    config.validate()
    assert config.profile.origin("auth") == "https://auth.platform.internal"
    assert config.references == {"CLIENT_ID", "CLIENT_SECRET"}


@pytest.mark.parametrize(
    "domain",
    [
        "https://foo.com",
        "foo.com.",
        "*.foo.com",
        "UPPER.com",
        "-foo.com",
        "foo..com",
        "x" * 64 + ".com",
    ],
)
def test_invalid_domains(config, domain):
    data = config.profile.model_dump()
    data["domain"] = domain
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_invalid_aks_gpu(config):
    data = config.profile.model_dump()
    data["platform"] = "aks"
    with pytest.raises(ValidationError, match="existing GPU"):
        Profile.model_validate(data)


def test_missing_secret_and_permissions(profile_path):
    secret_path = profile_path.parent / "secrets.yaml"
    private_write(secret_path, dump({}))
    with pytest.raises(StackError, match="Missing connector"):
        Configuration(profile_path).validate()
    os.chmod(secret_path, 0o644)
    with pytest.raises(StackError, match="chmod 600"):
        Configuration(profile_path)


@pytest.mark.parametrize("mode", ["azure", "cloudflare"])
def test_acme_fields(config, mode):
    data = config.profile.model_dump()
    data["tls"] = {
        "mode": mode,
        "acme_email": "admin@platform.internal",
        "dns_zone": "internal",
    }
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_cli_help_and_offline_validate(profile_path):
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("init", "validate", "preflight", "component", "addon"):
        assert command in result.output
    result = runner.invoke(app, ["validate", "-p", str(profile_path)])
    assert result.exit_code == 0, result.output
    assert "never-print-me" not in result.output


def test_validation_error_does_not_print_secret(profile_path):
    private_write(profile_path.parent / "secrets.yaml", "connector: never-print-me\n")
    result = CliRunner().invoke(app, ["validate", "-p", str(profile_path)])
    assert result.exit_code == 1
    assert "never-print-me" not in result.output


def test_init_self_signed_conditional_prompts(profile_path):
    result = CliRunner().invoke(
        app,
        ["init", "-p", str(profile_path)],
        input="\n".join(
            [
                "y",
                "dgx-spark",
                "test-only-never-contact",
                "operator",
                "auth",
                "librechat",
                "platform.internal",
                "self-signed",
                "file",
                str(profile_path.parent / "connector.yaml"),
                "",
                "",
                "",
                "save",
                "",
            ]
        ),
    )
    assert result.exit_code == 0, result.output
    assert "ACME registration email" not in result.output
    assert "Cloudflare API token" not in result.output
    assert "never-print-me" not in result.output
    assert read_yaml(profile_path)["tls"] == {"mode": "self-signed"}
    assert read_yaml(profile_path)["auth"]["chat_group"] == ""
    assert read_yaml(profile_path.parent / "inventory.yaml")["desired"] == {
        "components": ["auth"],
        "addons": ["librechat"],
    }


def test_init_review_can_switch_solver(profile_path):
    result = CliRunner().invoke(
        app,
        ["init", "-p", str(profile_path)],
        input="\n".join(
            [
                "y",
                "",
                "",
                "",
                "",
                "",
                "",
                "self-signed",
                "file",
                str(profile_path.parent / "connector.yaml"),
                "",
                "",
                "test:core",
                "test:users",
                "",
                "tls",
                "cloudflare",
                "admin@customer.net",
                "staging",
                "platform.internal",
                "hidden-cloudflare-token",
                "save",
                "",
            ]
        ),
    )
    assert result.exit_code == 0, result.output
    assert read_yaml(profile_path)["tls"]["environment"] == "staging"
    assert read_yaml(profile_path.parent / "secrets.yaml")["cloudflare_api_token"] == (
        "hidden-cloudflare-token"
    )
    assert "hidden-cloudflare-token" not in result.output
    assert "Azure client secret" not in result.output


def test_init_aks_defaults_to_components_without_gpu_operator(tmp_path):
    profile = tmp_path / "profile.yaml"
    result = CliRunner().invoke(
        app,
        ["init", "-p", str(profile)],
        input="aks\naks-context\nnone\nnone\nsave\n",
    )

    assert result.exit_code == 0, result.output
    assert "GPU management" not in result.output
    inventory = read_yaml(tmp_path / "inventory.yaml")
    assert inventory["desired"] == {"components": [], "addons": []}
    assert read_yaml(profile)["gpu"] == "existing"


def test_init_defaults_to_all_supported_components(tmp_path, monkeypatch):
    from enkictl import main

    monkeypatch.setattr(main, "REGISTRY", {"gpu-operator": object()})
    result = CliRunner().invoke(
        app,
        ["init", "-p", str(tmp_path / "profile.yaml")],
        input="dgx-spark\ntest-context\noperator\n\nnone\nsave\n",
    )

    assert result.exit_code == 0, result.output
    assert "Components to install" in result.output
    assert read_yaml(tmp_path / "inventory.yaml")["desired"] == {
        "components": ["gpu-operator"],
        "addons": [],
    }


def test_questionary_checkbox_marks_default_components_selected(monkeypatch):
    from enkictl import main

    captured = {}
    prompt = main.questionary.checkbox("choices", ["auth", "kserve"])
    prompt.ask = lambda: ["auth"]
    monkeypatch.setattr(main.sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())

    def checkbox(message, choices, **kwargs):
        captured["message"] = message
        captured["choices"] = choices
        captured["kwargs"] = kwargs
        captured["prompt"] = prompt
        return prompt

    monkeypatch.setattr(main.questionary, "checkbox", checkbox)

    assert main.prompt_selection("Components", ["auth", "kserve"], ["auth"]) == ["auth"]
    assert captured["message"] == "Components"
    assert [choice.checked for choice in captured["choices"]] == [True, False]
    assert captured["kwargs"]["instruction"] == (
        "Use arrow keys to move, <space> to select, <i> to invert"
    )
    bindings = captured["prompt"].application.key_bindings.bindings
    assert not any(binding.keys == ("a",) for binding in bindings)
    assert any(binding.keys == ("i",) for binding in bindings)


def test_literal_connector_secret_is_rejected_before_save(profile_path):
    path = profile_path.parent / "connector.yaml"
    connector = read_yaml(path)
    connector["connectors"][0]["config"]["clientSecret"] = "never-print-me"
    private_write(path, dump(connector))
    result = CliRunner().invoke(app, ["validate", "-p", str(profile_path)])
    assert result.exit_code == 1
    assert "must reference a secret" in result.output
    assert "never-print-me" not in result.output
