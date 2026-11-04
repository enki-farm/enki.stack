from __future__ import annotations

import json
import re
import secrets
from base64 import b64encode
from collections.abc import Callable
from pathlib import Path

from enkictl.addons.internal import InternalAddon
from enkictl.addons.librechat import _decode_secret
from enkictl.config import StackError, read_yaml
from enkictl.render import Release, resource

NAME = "activepieces"
CHART_VERSION = "0.6.0"
SECRET = "activepieces-credentials"
OIDC_SECRET = "activepieces-oidc"

GATEWAY_SCRIPT = """
const fs = require('node:fs');
async function main() {
    const { token, provider } = JSON.parse(fs.readFileSync(0, 'utf8'));
    async function request(path, body) {
        const response = await fetch('http://127.0.0.1:80/api/v1/ai-providers' + path, {
            method: body ? 'POST' : 'GET',
            headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
            ...(body ? { body: JSON.stringify(body) } : {}),
            signal: AbortSignal.timeout(30000),
        });
        if (!response.ok) {
            throw new Error('Activepieces provider API returned HTTP ' + response.status);
        }
        return response.json();
    }
    const configs = await request('/configs');
    if (!Array.isArray(configs)) {
        throw new Error('Activepieces provider API did not return a config array');
    }
    const matches = configs.filter(config => config.name === provider.displayName);
    if (matches.length > 1 || (matches.length === 1 && matches[0].provider !== 'custom')) {
        throw new Error('Enki Gateway provider name conflicts with an existing configuration');
    }
    if (matches.length === 1) {
        const { provider: _, ...update } = provider;
        await request('/' + encodeURIComponent(matches[0].id), update);
    } else {
        await request('', provider);
    }
    const saved = await request('/configs');
    if (!Array.isArray(saved)) {
        throw new Error('Activepieces provider API did not return a config array');
    }
    const configured = saved.filter(config =>
        config.name === provider.displayName && config.provider === provider.provider
    );
    if (configured.length !== 1 ||
        configured[0].config.baseUrl !== provider.config.baseUrl ||
        configured[0].config.apiKeyHeader !== provider.config.apiKeyHeader ||
        configured[0].config.models.length !== provider.config.models.length ||
        !provider.config.models.every((model, index) =>
            ['modelId', 'modelName', 'modelType'].every(key =>
                configured[0].config.models[index][key] === model[key]
            )
        )) {
        throw new Error('Enki Gateway provider configuration was not persisted');
    }
}
main().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
});
"""


class ActivepiecesAddon(InternalAddon):
    name = NAME
    dependencies = ("ai-gateway", "auth")
    secret_keys = (
        "AP_ENCRYPTION_KEY",
        "AP_JWT_SECRET",
        "AP_POSTGRES_PASSWORD",
        "AP_REDIS_PASSWORD",
    )

    def __init__(self, profile: Path, root: Path):
        super().__init__(profile, root)
        p = self.config.profile
        if not p.domain or not p.tls:
            raise StackError("Activepieces addon requires profile domain and tls configuration")
        self.deployment.releases = [
            Release(
                name=NAME,
                namespace=self.namespace,
                chart=str(root.resolve() / f"helm/activepieces/activepieces-{CHART_VERSION}.tgz"),
                version=CHART_VERSION,
                phase="addon",
                values=["helm/activepieces/values.yaml"],
                component=self.owner,
            )
        ]
        upstream = read_yaml(root / "helm/activepieces/values.yaml")
        self.deployment.overrides[NAME] = {
            "image": {
                "repository": "ghcr.io/enki-farm/activepieces",
                "tag": "latest",
                "pullPolicy": "Always",
            },
            "workloadType": "statefulset",
            "kubernetes-secret-generator": {"enabled": False},
            "persistence": {"storageClassName": p.storage_class},
            "activepiecesConfig": {
                "AP_EDITION": "ce",
                "AP_TELEMETRY_ENABLED": "false",
                "AP_ENVIRONMENT": "prod",
                "AP_EXECUTION_MODE": "SANDBOX_CODE_ONLY",
                "AP_FRONTEND_URL": p.origin("ap"),
                "AP_DB_TYPE": "POSTGRES",
                "AP_POSTGRES_HOST": "activepieces-postgresql",
                "AP_POSTGRES_PORT": "5432",
                "AP_POSTGRES_DATABASE": NAME,
                "AP_POSTGRES_USERNAME": NAME,
                "AP_REDIS_TYPE": "STANDALONE",
                "AP_REDIS_HOST": "activepieces-redis-master",
                "AP_REDIS_PORT": "6379",
                "AP_QUEUE_UI_ENABLED": "false",
                "AP_PROXY_AUTH_ENABLED": "true",
                "AP_PROXY_AUTH_BOOTSTRAP_ADMIN_EMAIL": "marco@enki.swiss",
            },
            # Remove upstream optional secret mappings before adding our required credentials.
            "activepiecesEnvVariables": {
                **{name: None for name in upstream["activepiecesEnvVariables"]},
                SECRET: list(self.secret_keys),
            },
            "postgresql": {
                "enabled": True,
                "image": {"tag": "14.9.0-debian-11-r61"},
                "auth": {
                    "database": NAME,
                    "username": NAME,
                    "existingSecret": SECRET,
                    "secretKeys": {
                        "adminPasswordKey": "AP_POSTGRES_PASSWORD",
                        "userPasswordKey": "AP_POSTGRES_PASSWORD",
                    },
                },
                "primary": {"persistence": {"storageClass": p.storage_class}},
            },
            "redis": {
                "enabled": True,
                "image": {"tag": "7.0.15-debian-12-r20"},
                "architecture": "standalone",
                "auth": {
                    "existingSecret": SECRET,
                    "existingSecretPasswordKey": "AP_REDIS_PASSWORD",
                },
                "master": {"persistence": {"storageClass": p.storage_class}},
            },
        }
        if p.tls.mode == "self-signed":
            self.deployment.overrides[NAME]["activepiecesConfig"]["NODE_EXTRA_CA_CERTS"] = (
                "/etc/enki-trust/ca.crt"
            )
            self.deployment.overrides[NAME]["volumes"] = [
                {"name": "platform-trust", "configMap": {"name": "enki-platform-trust"}}
            ]
            self.deployment.overrides[NAME]["volumeMounts"] = [
                {"name": "platform-trust", "mountPath": "/etc/enki-trust", "readOnly": True}
            ]

    def _credentials(self) -> dict[str, str]:
        existing = _decode_secret(self.runner.get("secret", SECRET, self.namespace))
        local = self.config.secrets.addons.get(NAME, {})
        values = {}
        for key in self.secret_keys:
            requested, stored = local.get(key, ""), existing.get(key, "")
            if requested and stored and requested != stored:
                raise StackError(f"Configured {NAME} credential differs from {SECRET}:{key}")
            values[key] = (
                requested or stored or secrets.token_hex(16 if key == "AP_ENCRYPTION_KEY" else 32)
            )
        if not re.fullmatch(r"[a-fA-F0-9]{32}", values["AP_ENCRYPTION_KEY"]):
            raise StackError(
                "Activepieces AP_ENCRYPTION_KEY must contain 32 hexadecimal characters"
            )
        if len(values["AP_JWT_SECRET"]) < 32:
            raise StackError("Activepieces AP_JWT_SECRET must contain at least 32 characters")
        return values

    def _configure_gateway(self) -> None:
        local = self.config.secrets.addons.get(NAME, {})
        token = local.get("ACTIVEPIECES_API_KEY", "")
        if not token:
            raise StackError(
                "Set addons.activepieces.ACTIVEPIECES_API_KEY in profile secrets.yaml "
                "to an Activepieces platform-admin/service API key, then rerun the addon install"
            )
        self.runner.kubectl(
            "exec", "-i", "-n", self.namespace, "statefulset/activepieces",
            "--", "node", "-e", GATEWAY_SCRIPT,
            data=json.dumps({
                "token": token,
                "provider": {
                    "displayName": "Enki Gateway",
                    "provider": "custom",
                    "auth": {
                        "apiKey": "Bearer " + local.get("ENKI_GATEWAY_API_KEY", "sk-local-vllm")
                    },
                    "config": {
                        "baseUrl": self.config.profile.origin("ai") + "/v1",
                        "apiKeyHeader": "Authorization",
                        "apiStyle": "chat",
                        "models": [
                            {"modelId": "default", "modelName": "Enki", "modelType": "text"}
                        ],
                    },
                },
            }),
        )

    def check(
        self, progress: Callable[[str], None] | None = None
    ) -> tuple[dict[str, str], str, list[str]]:
        for dependency in self.dependencies:
            if (
                self.installation.state["components"].get(dependency, {}).get("status")
                != "installed"
            ):
                raise StackError(
                    f"{self.name} requires the installed {dependency} component "
                    "in this profile inventory"
                )
        dex_secret = _decode_secret(self.runner.get("secret", "dex-secrets", "dex"))
        client_secret = dex_secret.get("ACTIVEPIECES_CLIENT_SECRET")
        if not client_secret:
            raise StackError(
                "Activepieces Dex client secret is missing; rerun `enkictl component install auth`"
            )
        oidc_secret = resource(
            "Secret",
            OIDC_SECRET,
            self.namespace,
            type="Opaque",
            data={"client-secret": b64encode(client_secret.encode()).decode()},
        )
        content = [
            doc
            for doc in self.deployment.resources["content"]
            if not (doc["kind"] == "Secret" and doc["metadata"].get("name") == OIDC_SECRET)
        ]
        self.deployment.resources["content"] = [oidc_secret, *content]
        self.runner.kubectl("get", "storageclass", self.config.profile.storage_class, "-o", "name")
        values, password, warnings = super().check(progress)
        warnings.append(
            "Activepieces Community Edition runs flows in unsandboxed mode; only trusted users "
            "should have access. Dex login is handled by the Envoy Gateway policy."
        )
        local = self.config.secrets.addons.get(NAME, {})
        if not local.get("ACTIVEPIECES_API_KEY"):
            warnings.append(
                "Gateway provisioning requires addons.activepieces.ACTIVEPIECES_API_KEY in "
                "profile secrets.yaml. On first install, obtain a platform-admin/service API "
                "key after logging in, save it locally and rerun the addon install."
            )
        if not local.get("ENKI_GATEWAY_API_KEY"):
            warnings.append(
                "Using placeholder gateway API key; set addons.activepieces.ENKI_GATEWAY_API_KEY "
                "if gateway authentication is enabled."
            )
        return values, password, warnings

    def install(self, values: dict[str, str], _db_password: str = "") -> None:
        self.config.secrets.addons.setdefault(NAME, {}).update(values)
        self.config.save_secrets()
        state = self.installation.state["addons"].setdefault(NAME, {})
        state["status"] = "installing"
        self.installation.save()
        try:
            self.installation.bind(write=True, configuration=True)
            self.installation.component = self.owner
            if not self.runner.get("namespace", self.namespace):
                self.installation.apply(resource("Namespace", self.namespace), "namespaces")
            if self.config.profile.tls.mode == "self-signed":
                self.runner.kubectl(
                    "label", "namespace", self.namespace, "enki.stack/trust=true", "--overwrite"
                )
                self.runner.kubectl(
                    "wait",
                    "configmap/enki-platform-trust",
                    "-n",
                    self.namespace,
                    "--for=jsonpath={.data.ca\\.crt}",
                    "--timeout=180s",
                )
            for doc in self.deployment.resources["credentials"]:
                self.installation.apply(doc, "addon")
            content = self.deployment.resources["content"]
            for doc in content:
                self.installation.apply(doc, "addon")
            self.installation.helm_install(self.deployment.releases[0])
            self.installation.component = self.owner
            self.installation.wait_resources(content)
            self.installation.capture_generated()
            self._configure_gateway()
            state["status"] = "installed"
            self.installation.state["status"] = "installed"
            self.installation.save()
        except StackError as exc:
            state["status"] = "failed"
            self.installation.save()
            raise StackError(
                f"Activepieces addon install failed; partial inventory retained at "
                f"{self.installation.path}. Correct the cause and rerun: {exc}"
            ) from None
        finally:
            self.installation.component = None
