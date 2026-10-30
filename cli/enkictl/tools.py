from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from enkictl.config import StackError


def _command_diagnostics(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    sections = []
    for label, output in (("stderr", stderr), ("stdout", stdout)):
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        if output and output.strip():
            sections.append(f"{label}:\n{output.rstrip()}")
    return "\n".join(sections) or "The command returned no diagnostic output."


class Runner:
    def __init__(self, context: str):
        self.context = context
        self.installation: str | None = None
        self.component: str | None = None
        self.helm_major: int | None = None
        self.helm_exclude_crds: list[str] = []

    def run(self, args: list[str], data: str | None = None, timeout: int = 900) -> str:
        try:
            result = subprocess.run(
                args,
                input=data,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env={
                    **os.environ,
                    **(
                        {"HELM_PLUGINS": str(Path(__file__).parent / "helm_plugins")}
                        if self.helm_major == 4
                        else {}
                    ),
                    **({"ENKI_INSTALLATION_ID": self.installation} if self.installation else {}),
                    **({"ENKI_COMPONENT": self.component} if self.component else {}),
                    "ENKI_HELM_EXCLUDE_CRDS": ",".join(self.helm_exclude_crds),
                },
            )
        except FileNotFoundError:
            raise StackError(f"Required executable not found: {args[0]}") from None
        except subprocess.TimeoutExpired as exc:
            raise StackError(
                f"{args[0]} timed out after {timeout}s.\n"
                f"{_command_diagnostics(exc.stdout, exc.stderr)}"
            ) from None
        if result.returncode:
            operation = next(
                (
                    arg
                    for arg in args[1:]
                    if arg
                    in {
                        "template",
                        "upgrade",
                        "apply",
                        "get",
                        "wait",
                        "delete",
                        "version",
                        "rollout",
                        "label",
                        "patch",
                        "list",
                        "uninstall",
                        "kustomize",
                        "config",
                        "auth",
                        "api-versions",
                    }
                ),
                "command",
            )
            raise StackError(
                f"{args[0]} {operation} failed (exit {result.returncode}).\n"
                f"{_command_diagnostics(result.stdout, result.stderr)}"
            )
        return result.stdout

    def kubectl(self, *args: str, data: str | None = None) -> str:
        try:
            return self.run(
                ["kubectl", "--context", self.context, "--request-timeout=30s", *args], data
            )
        except StackError as exc:
            if args and args[0] == "get":
                raise StackError(
                    f"{exc} Failed request: {self._get_location(args)} (context {self.context})."
                ) from None
            raise

    @staticmethod
    def _get_location(args: tuple[str, ...]) -> str:
        targets = args[1:]
        raw = next((arg for arg in targets if arg.startswith("--raw=")), None)
        if raw:
            return "kubectl get API endpoint"

        resource = next((arg for arg in targets if not arg.startswith("-")), "resource list")
        name = targets[1] if len(targets) > 1 and targets[0] == resource else None
        if (
            name
            and not name.startswith("-")
            and resource.lower() not in {"secret", "secrets", "configmap", "configmaps"}
        ):
            resource = f"{resource}/{name}"

        scope = " --all-namespaces" if "--all-namespaces" in targets else ""
        return f"kubectl get {resource}{scope}"

    def json(self, *args: str) -> dict:
        try:
            return json.loads(self.kubectl(*args, "-o", "json"))
        except json.JSONDecodeError:
            raise StackError("kubectl returned invalid JSON") from None

    def get(self, kind: str, name: str, namespace: str = "") -> dict | None:
        args = ["get", kind, name, "--ignore-not-found", "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        raw = self.kubectl(*args)
        return json.loads(raw) if raw.strip() else None

    def render(self, path: Path) -> str:
        if shutil.which("kustomize"):
            return self.run(
                ["kustomize", "build", "--load-restrictor=LoadRestrictionsNone", str(path)]
            )
        return self.kubectl("kustomize", "--load-restrictor=LoadRestrictionsNone", str(path))

    def helm(self, *args: str, data: str | None = None) -> str:
        if args and args[0] == "list" and "--all" in args:
            # Helm 4 removed --all; explicit status filters work on Helm 3 and 4.
            index = args.index("--all")
            args = (
                *args[:index],
                "--deployed",
                "--failed",
                "--pending",
                "--uninstalled",
                "--uninstalling",
                "--superseded",
                *args[index + 1 :],
            )
        return self.run(["helm", "--kube-context", self.context, *args], data)

    def post_renderer(self) -> str:
        executable = shutil.which("enki-stack-helm-renderer")
        if not executable:
            raise StackError("Missing Helm post-renderer; reinstall enkictl")
        version = self.run(["helm", "version", "--template", "{{.Version}}"]).strip()
        if version.startswith("v4."):
            self.helm_major = 4
            return "enki-stack"
        if version.startswith("v3."):
            self.helm_major = 3
            return executable
        raise StackError("Supported Helm versions are 3.x and 4.x")
