import subprocess

import pytest

from enkictl.config import StackError
from enkictl.tools import Runner


def test_kubectl_get_failure_reports_resource_and_tool_output(monkeypatch):
    def failed_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["kubectl"], returncode=1, stdout="", stderr="sensitive tool output"
        )

    monkeypatch.setattr("enkictl.tools.subprocess.run", failed_run)

    with pytest.raises(StackError) as error:
        Runner("production").kubectl("get", "pods", "--all-namespaces", "-o", "json")

    message = str(error.value)
    assert "kubectl get pods --all-namespaces (context production)" in message
    assert "stderr:\nsensitive tool output" in message


def test_kubectl_get_secret_failure_does_not_report_secret_name(monkeypatch):
    def failed_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["kubectl"], returncode=1, stdout="", stderr="")

    monkeypatch.setattr("enkictl.tools.subprocess.run", failed_run)

    with pytest.raises(StackError) as error:
        Runner("production").kubectl("get", "secret", "private-credentials", "-n", "auth")

    message = str(error.value)
    assert "kubectl get secret (context production)" in message
    assert "private-credentials" not in message


def test_kubectl_get_failure_reports_named_resource(monkeypatch):
    def failed_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["kubectl"], returncode=1, stdout="", stderr="")

    monkeypatch.setattr("enkictl.tools.subprocess.run", failed_run)

    with pytest.raises(StackError) as error:
        Runner("production").kubectl("get", "namespace", "kube-system")

    assert "kubectl get namespace/kube-system (context production)" in str(error.value)


def test_kubectl_wait_failure_includes_full_diagnostic(monkeypatch):
    def failed_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["kubectl"],
            returncode=1,
            stdout="",
            stderr=(
                "error: timed out waiting for the condition on pods/kserve-controller\n"
                "client_secret=top-secret token: bearer-value\n"
                "-----BEGIN PRIVATE KEY-----\nprivate-key-value\n-----END PRIVATE KEY-----"
            ),
        )

    monkeypatch.setattr("enkictl.tools.subprocess.run", failed_run)

    with pytest.raises(StackError) as error:
        Runner("production").kubectl("wait", "--for=condition=Ready", "pods/kserve-controller")

    message = str(error.value)
    assert "timed out waiting for the condition on pods/kserve-controller" in message
    assert "client_secret=top-secret token: bearer-value" in message
    assert "-----BEGIN PRIVATE KEY-----\nprivate-key-value\n-----END PRIVATE KEY-----" in message


@pytest.mark.parametrize(
    "command",
    [
        ["kubectl", "apply", "-f", "-"],
        ["kubectl", "patch", "deployment", "controller"],
        ["helm", "upgrade", "kserve", "chart"],
        ["kustomize", "build", "."],
    ],
)
def test_command_failure_includes_complete_output(monkeypatch, command):
    stderr = (
        "Error from server (Invalid): error when creating \"STDIN\":\n"
        + "  validation detail\n" * 12
        + "x" * 800
        + "\nCustomResourceDefinition is invalid: metadata.annotations: Too long"
    )
    stdout = "deployment.apps/kserve-controller configured\n"
    monkeypatch.setattr(
        "enkictl.tools.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(command, 1, stdout, stderr),
    )

    with pytest.raises(StackError) as error:
        Runner("production").run(command, data="stdin-is-not-logged")

    message = str(error.value)
    assert "exit 1" in message
    assert f"stderr:\n{stderr}" in message
    assert f"stdout:\n{stdout.rstrip()}" in message
    assert "stdin-is-not-logged" not in message
    assert "Raw output withheld" not in message


def test_failure_without_output_is_explicit(monkeypatch):
    monkeypatch.setattr(
        "enkictl.tools.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["kubectl"], 1, "", ""),
    )

    with pytest.raises(StackError, match="The command returned no diagnostic output"):
        Runner("production").kubectl("apply", "-f", "-")


@pytest.mark.parametrize("as_bytes", [False, True])
def test_timeout_includes_partial_output(monkeypatch, as_bytes):
    def timed_out(args, **kwargs):
        stdout = "deployment.apps/kserve-controller configured"
        stderr = "Error from server: webhook connection refused"
        raise subprocess.TimeoutExpired(
            args,
            kwargs["timeout"],
            output=stdout.encode() if as_bytes else stdout,
            stderr=stderr.encode() if as_bytes else stderr,
        )

    monkeypatch.setattr("enkictl.tools.subprocess.run", timed_out)

    with pytest.raises(StackError) as error:
        Runner("production").run(["kubectl", "apply", "-f", "-"], timeout=10)

    message = str(error.value)
    assert "timed out after 10s" in message
    assert "stderr:\nError from server: webhook connection refused" in message
    assert "stdout:\ndeployment.apps/kserve-controller configured" in message


@pytest.mark.parametrize(
    ("version", "renderer"),
    [
        ("v3.18.0", "/tools/enki-stack-helm-renderer"),
        ("v4.3.1", "enki-stack"),
    ],
)
def test_postrenderer_helm_major_selection(monkeypatch, version, renderer):
    runner = Runner("test-only")
    monkeypatch.setattr("enkictl.tools.shutil.which", lambda _: "/tools/enki-stack-helm-renderer")
    monkeypatch.setattr(runner, "run", lambda _: version)
    assert runner.post_renderer() == renderer
    assert runner.helm_major == int(version[1])


def test_component_identity_is_passed_to_subprocess_only(monkeypatch):
    import os

    before = os.environ.get("ENKI_COMPONENT")
    captured = {}

    def run(args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("enkictl.tools.subprocess.run", run)
    runner = Runner("test-only")
    runner.component = "monitoring"
    runner.installation = "installation-id"
    runner.helm_exclude_crds = ["shared.example.test"]
    runner.run(["helm", "version"])
    assert captured["ENKI_COMPONENT"] == "monitoring"
    assert captured["ENKI_INSTALLATION_ID"] == "installation-id"
    assert captured["ENKI_HELM_EXCLUDE_CRDS"] == "shared.example.test"
    assert os.environ.get("ENKI_COMPONENT") == before


@pytest.mark.parametrize("major", [None, 3, 4])
def test_helm_list_all_uses_portable_status_flags(monkeypatch, major):
    runner = Runner("test-only")
    runner.helm_major = major
    captured = []
    monkeypatch.setattr(runner, "run", lambda args, data=None: captured.append(args) or "[]")

    assert runner.helm("list", "--namespace", "auth", "--all", "--output", "json") == "[]"
    assert captured == [
        [
            "helm",
            "--kube-context",
            "test-only",
            "list",
            "--namespace",
            "auth",
            "--deployed",
            "--failed",
            "--pending",
            "--uninstalled",
            "--uninstalling",
            "--superseded",
            "--output",
            "json",
        ]
    ]


@pytest.mark.parametrize(
    "args",
    [
        ("list", "--all-namespaces", "--output", "json"),
        ("list", "--failed", "--output", "json"),
        ("uninstall", "test-release", "--namespace", "auth"),
    ],
)
def test_helm_preserves_other_commands_and_filters(monkeypatch, args):
    runner = Runner("test-only")
    captured = []
    monkeypatch.setattr(runner, "run", lambda command, data=None: captured.append(command) or "")

    runner.helm(*args)
    assert captured == [["helm", "--kube-context", "test-only", *args]]
