from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from pathlib import Path

from enkictl.addons.librechat import _decode_secret
from enkictl.checks import preflight as check_prerequisites
from enkictl.config import Configuration, StackError
from enkictl.lifecycle import Installation
from enkictl.render import Deployment, documents, resource
from enkictl.tools import Runner


class InternalAddon:
    dependencies: tuple[str, ...] = ()
    addon_dependencies: tuple[str, ...] = ()
    name: str
    secret_keys: tuple[str, ...]

    def __init__(self, profile: Path, root: Path):
        self.config = Configuration(profile, load_connector=False, targets=[])
        self.config.validate()
        self.namespace = self.name
        self.owner = f"addon:{self.name}"
        self.secret_name = f"{self.name}-credentials"
        self.deployment = Deployment(
            self.config, root.resolve(), Runner(self.config.profile.context), targets=[]
        )
        self.installation = Installation(self.deployment)
        self.runner = self.deployment.runner
        self.deployment.resources["content"] = [
            self.deployment.transform(doc)
            for doc in documents(self.runner.render(root / "k8s/addons" / self.name))
        ]

    def _credentials(self) -> dict[str, str]:
        existing = _decode_secret(self.runner.get("secret", self.secret_name, self.namespace))
        local = self.config.secrets.addons.get(self.name, {})
        values = {}
        for key in self.secret_keys:
            requested, stored = local.get(key, ""), existing.get(key, "")
            if requested and stored and requested != stored:
                raise StackError(
                    f"Configured {self.name} credential differs from {self.secret_name}:{key}"
                )
            values[key] = (
                requested
                or stored
                or ("sk-local-vllm" if key == "OPENAI_API_KEY" else secrets.token_hex(32))
            )
        return values

    def check(
        self, progress: Callable[[str], None] | None = None
    ) -> tuple[dict[str, str], str, list[str]]:
        for name in self.dependencies:
            if self.installation.state["components"].get(name, {}).get("status") != "installed":
                raise StackError(
                    f"{self.name} requires the installed {name} component in this profile inventory"
                )
            self.installation.verify_dependency(name)
        values = self._credentials()
        self.deployment.resources["credentials"] = [
            resource(
                "Secret",
                self.secret_name,
                self.namespace,
                type="Opaque",
                data={
                    key: base64.b64encode(value.encode()).decode() for key, value in values.items()
                },
            )
        ]
        self.deployment.acting = [self.owner]
        try:
            warnings = check_prerequisites(self.deployment, progress=progress)
        finally:
            self.deployment.acting = []
        self.installation.conflicts()
        if values.get("OPENAI_API_KEY") == "sk-local-vllm":
            warnings.append(
                "Using placeholder gateway API key; set addons.crawl4ai.OPENAI_API_KEY "
                "if gateway authentication is enabled."
            )
        return values, "", warnings

    def install(self, values: dict[str, str], _db_password: str = "") -> None:
        self.config.secrets.addons.setdefault(self.name, {}).update(values)
        self.config.save_secrets()
        state = self.installation.state["addons"].setdefault(self.name, {})
        state["status"] = "installing"
        self.installation.save()
        try:
            self.installation.bind(write=True, configuration=True)
            self.installation.component = self.owner
            namespace = resource("Namespace", self.namespace)
            if not self.runner.get("namespace", self.namespace):
                self.installation.apply(namespace, "namespaces")
            for doc in self.deployment.resources["credentials"]:
                self.installation.apply(doc, "addon")
            content = self.deployment.resources["content"]
            for doc in content:
                if doc["kind"] != "Namespace":
                    self.installation.apply(doc, "addon")
            self.installation.wait_resources(content)
            state["status"] = "installed"
            self.installation.state["status"] = "installed"
            self.installation.save()
        except StackError as exc:
            state["status"] = "failed"
            self.installation.save()
            raise StackError(
                f"{self.name} addon install failed; partial inventory retained at "
                f"{self.installation.path}. Correct the cause and rerun: {exc}"
            ) from None
        finally:
            self.installation.component = None


class SearxngAddon(InternalAddon):
    name = "searxng"
    secret_keys = ("SEARXNG_SECRET",)


class Crawl4aiAddon(InternalAddon):
    name = "crawl4ai"
    dependencies = ("ai-gateway",)
    secret_keys = ("CRAWL4AI_API_TOKEN", "FIRECRAWL_SHIM_API_KEY", "OPENAI_API_KEY")
