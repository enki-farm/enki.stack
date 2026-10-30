from __future__ import annotations

import base64
import re
import secrets as random_secrets
import tarfile
from base64 import b64encode
from collections.abc import Callable
from pathlib import Path

import yaml

from enkictl.checks import preflight as check_prerequisites
from enkictl.config import Configuration, StackError
from enkictl.lifecycle import Installation
from enkictl.render import Deployment, Release, documents, resource
from enkictl.tools import Runner

NAME = "librechat"
OWNER = f"addon:{NAME}"
NAMESPACE = "librechat"
RELEASE = "librechat"
CHART = "oci://ghcr.io/librechat-ai/librechat-chart/librechat"
CHART_VERSION = "2.0.17"
RAG_IMAGE = (
    "registry.librechat.ai/librechat-ai/librechat-rag-api-dev-lite"
    "@sha256:a62ce2221b43a9bb57fa6cb733e7047c80132960272c33d5704028f3071a1251"
)
CHAT_SECRET = "librechat-credentials-env"
RAG_SECRET = "librechat-rag-db-credentials"
CRAWL_NAMESPACE = "crawl4ai"
CRAWL_SECRET = "crawl4ai-credentials"
SECRET_KEYS = (
    "ENKI_GATEWAY_API_KEY",
    "CREDS_KEY",
    "CREDS_IV",
    "JWT_SECRET",
    "JWT_REFRESH_SECRET",
    "MEILI_MASTER_KEY",
    "FIRECRAWL_API_KEY",
    "CRAWL4AI_API_TOKEN",
    "OPENID_CLIENT_SECRET",
    "OPENID_SESSION_SECRET",
)


def _decode_secret(secret: dict | None) -> dict[str, str]:
    if not secret:
        return {}
    if not isinstance(secret, dict):
        raise StackError("Existing addon Secret contains invalid encoded data")
    data = secret.get("data", {})
    if not isinstance(data, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in data.items()
    ):
        raise StackError("Existing addon Secret contains invalid encoded data")
    try:
        return {key: base64.b64decode(value, validate=True).decode() for key, value in data.items()}
    except (ValueError, UnicodeDecodeError):
        raise StackError("Existing addon Secret contains invalid encoded data") from None


def _replace_domain(value: object, domain: str) -> object:
    if isinstance(value, dict):
        return {key: _replace_domain(child, domain) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_domain(child, domain) for child in value]
    if isinstance(value, str):
        return value.replace("zer0.garden", domain)
    return value


def _validate_archive(path: Path) -> None:
    if not path.is_file():
        raise StackError("Uploads archive does not exist")
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive:
                parts = Path(member.name).parts
                if (
                    not member.name
                    or Path(member.name).is_absolute()
                    or ".." in parts
                    or not (member.isfile() or member.isdir())
                ):
                    raise StackError("Uploads archive contains an unsafe path or link")
    except (OSError, tarfile.TarError):
        raise StackError("Uploads archive is not a readable tar archive") from None


class LibreChatAddon:
    dependencies = ("ai-gateway", "kserve", "auth")
    addon_dependencies = ("searxng", "crawl4ai")

    def __init__(
        self,
        profile: Path,
        root: Path,
        namespace: str = NAMESPACE,
        chart_version: str = CHART_VERSION,
        rag_image: str = RAG_IMAGE,
        uploads_archive: Path | None = None,
    ):
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace) or len(namespace) > 63:
            raise StackError("Invalid addon namespace")
        if not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]*", chart_version):
            raise StackError("Invalid LibreChat chart version")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:@_-]+", rag_image):
            raise StackError("Invalid RAG image reference")

        self.config = Configuration(profile, load_connector=False, targets=[])
        self.config.validate()
        if self.config.profile.platform != "dgx-spark":
            raise StackError("LibreChat addon currently supports only dgx-spark")
        if not self.config.profile.domain or not self.config.profile.tls:
            raise StackError("LibreChat addon requires profile domain and tls configuration")
        auth = self.config.profile.auth
        if not auth or not (auth.chat_group or auth.hub_group):
            raise StackError("LibreChat requires auth.chat_group or auth.hub_group for Dex access")
        self.chat_group = auth.chat_group or auth.hub_group
        self.root = root.resolve()
        self.namespace = namespace
        self.uploads_archive = uploads_archive.resolve() if uploads_archive else None
        self.deployment = Deployment(
            self.config, self.root, Runner(self.config.profile.context), targets=[]
        )
        self.installation = Installation(self.deployment)
        self.runner = self.deployment.runner
        self.chart_version = chart_version
        self.rag_image = rag_image
        self.external_credentials: set[str] = set()
        self._configure()

    def _configure(self) -> None:
        p = self.config.profile
        rendered_rag = self.runner.render(self.root / "k8s/addons/librechat/rag")
        self.deployment.resources["content"] = documents(rendered_rag)
        for doc in self.deployment.resources["content"]:
            doc.setdefault("metadata", {}).setdefault("namespace", self.namespace)
            if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "librechat-rag-api":
                doc["spec"]["template"]["spec"]["containers"][0]["image"] = self.rag_image
        route = documents((self.root / "k8s/addons/librechat/httproute.yaml").read_text())
        route[0]["spec"]["hostnames"] = [f"chat.{p.domain}"]
        route[0]["metadata"]["namespace"] = self.namespace
        self.deployment.resources["content"].extend(route)

        values = yaml.safe_load((self.root / "k8s/addons/librechat/values.yaml").read_text())
        values = _replace_domain(values, p.domain)
        values["global"]["librechat"]["existingSecretName"] = CHAT_SECRET
        values["librechat"]["existingSecretName"] = CHAT_SECRET
        values["librechat"]["configEnv"].update(
            {
                "DOMAIN_CLIENT": f"https://chat.{p.domain}",
                "DOMAIN_SERVER": f"https://chat.{p.domain}",
                "RAG_API_URL": "http://librechat-rag-api:8000",
                "ALLOW_EMAIL_LOGIN": "false",
                "ALLOW_EMAIL_LOGIN_OVERRIDE": "false",
                "ALLOW_REGISTRATION": "false",
                "ALLOW_SOCIAL_LOGIN": "true",
                "ALLOW_SOCIAL_REGISTRATION": "true",
                "ALLOW_SHARED_LINKS_PUBLIC": "false",
                "OPENID_CLIENT_ID": "librechat",
                "OPENID_ISSUER": p.origin("auth"),
                "OPENID_CALLBACK_URL": "/oauth/openid/callback",
                "OPENID_SCOPE": "openid profile email groups offline_access",
                "OPENID_USE_PKCE": "true",
                "OPENID_GENERATE_NONCE": "true",
                "OPENID_AUTO_REDIRECT": "true",
                "OPENID_REQUIRED_ROLE": self.chat_group,
                "OPENID_REQUIRED_ROLE_TOKEN_KIND": "id",
                "OPENID_REQUIRED_ROLE_PARAMETER_PATH": "groups",
                "OPENID_REUSE_TOKENS": "false",
            }
        )
        values["librechat"]["configYamlContent"] += '\nregistration:\n  socialLogins: ["openid"]\n'
        if p.tls and p.tls.mode == "self-signed":
            values["librechat"]["configEnv"]["NODE_EXTRA_CA_CERTS"] = "/etc/enki-trust/ca.crt"
            values.setdefault("volumes", []).append(
                {"name": "platform-trust", "configMap": {"name": "enki-platform-trust"}}
            )
            values.setdefault("volumeMounts", []).append(
                {"name": "platform-trust", "mountPath": "/etc/enki-trust", "readOnly": True}
            )
        self.deployment.overrides[RELEASE] = values
        release = Release(
            name=RELEASE,
            namespace=self.namespace,
            chart=CHART,
            version=self.chart_version,
            phase="addon",
            values=[],
            component=OWNER,
        )
        self.deployment.releases = [release]

    def _credentials(self) -> tuple[dict[str, str], str]:
        existing_chat = _decode_secret(self.runner.get("secret", CHAT_SECRET, self.namespace))
        existing_db_secret = self.runner.get("secret", RAG_SECRET, self.namespace)
        existing_db = _decode_secret(existing_db_secret).get("POSTGRES_PASSWORD", "")
        pvc = self.runner.get("persistentvolumeclaim", "data-librechat-rag-db-0", self.namespace)
        if pvc and not existing_db:
            raise StackError(
                "RAG database PVC exists without its credentials Secret; "
                "restore the original Secret"
            )

        local = self.config.secrets.addons.setdefault(NAME, {})
        self.external_credentials.clear()
        dex_values = _decode_secret(self.runner.get("secret", "dex-secrets", "dex"))
        client_secret = dex_values.get("LIBRECHAT_CLIENT_SECRET")
        if not client_secret:
            raise StackError(
                "Dex LibreChat client is not configured; run enkictl component install auth "
                "with this profile first"
            )
        crawl_values = {}
        if any(not local.get(key) for key in ("FIRECRAWL_API_KEY", "CRAWL4AI_API_TOKEN")):
            crawl_values = _decode_secret(self.runner.get("secret", CRAWL_SECRET, CRAWL_NAMESPACE))
        values: dict[str, str] = {}
        for key in SECRET_KEYS:
            requested = local.get(key, "")
            stored = existing_chat.get(key, "")
            if requested and stored and requested != stored:
                raise StackError(
                    f"Configured LibreChat credential differs from {CHAT_SECRET}:{key}"
                )
            value = requested
            if key == "OPENID_CLIENT_SECRET":
                if any(value and value != client_secret for value in (requested, stored)):
                    raise StackError(
                        "LibreChat OIDC client credential differs from Dex; restore matching "
                        "credentials before reinstalling"
                    )
                value = client_secret
                self.external_credentials.add(key)
            if not value and key == "FIRECRAWL_API_KEY":
                value = crawl_values.get("FIRECRAWL_SHIM_API_KEY", "")
                if value:
                    self.external_credentials.add(key)
            if not value and key == "CRAWL4AI_API_TOKEN":
                value = crawl_values.get("CRAWL4AI_API_TOKEN", "")
                if value:
                    self.external_credentials.add(key)
            value = value or stored
            if not value and key == "ENKI_GATEWAY_API_KEY":
                value = "sk-local-vllm"
            if not value and key in {
                "CREDS_IV",
                "CREDS_KEY",
                "JWT_SECRET",
                "JWT_REFRESH_SECRET",
                "MEILI_MASTER_KEY",
                "OPENID_SESSION_SECRET",
            }:
                value = random_secrets.token_hex(16 if key == "CREDS_IV" else 32)
            if value:
                values[key] = value
        if not values.get("CRAWL4AI_API_TOKEN"):
            raise StackError(
                f"Missing {CRAWL_SECRET} Secret in {CRAWL_NAMESPACE}; install Crawl4AI first"
            )

        requested_db = local.get("RAG_POSTGRES_PASSWORD", "")
        if existing_db and requested_db and existing_db != requested_db:
            raise StackError(
                "Configured RAG database password differs from the existing Secret; "
                "rotate the PostgreSQL role explicitly"
            )
        db_password = existing_db or requested_db or random_secrets.token_hex(32)
        return values, db_password

    def preflight(self) -> tuple[dict[str, str], str]:
        for name in self.dependencies:
            if self.installation.state["components"].get(name, {}).get("status") != "installed":
                raise StackError(
                    f"LibreChat requires the installed {name} component in this profile inventory"
                )
            self.installation.verify_dependency(name)
        dex_config = self.runner.get("configmap", "dex-config", "dex")
        clients = (
            documents(dex_config["data"]["config.yaml"])[0].get("staticClients", [])
            if dex_config
            else []
        )
        if not any(
            client.get("id") == "librechat"
            and client.get("secretEnv") == "LIBRECHAT_CLIENT_SECRET"
            and self.config.profile.origin("chat") + "/oauth/openid/callback"
            in client.get("redirectURIs", [])
            for client in clients
        ):
            raise StackError(
                "Dex LibreChat callback is not registered; run enkictl component install auth "
                "with this profile first"
            )
        self.runner.kubectl("get", "storageclass", self.config.profile.storage_class, "-o", "name")
        for service_namespace, service_name in (
            ("searxng", "searxng"),
            (CRAWL_NAMESPACE, "crawl4ai"),
        ):
            if not self.runner.get("service", service_name, service_namespace):
                raise StackError(f"LibreChat requires Service {service_namespace}/{service_name}")
        model = self.runner.get("inferenceservice", "bge-embeddings", "inference")
        if not model or not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in model.get("status", {}).get("conditions", [])
        ):
            raise StackError(
                "BGE embedding model is not Ready; install models/bge-embeddings first"
            )
        values, db_password = self._credentials()
        if self.uploads_archive:
            _validate_archive(self.uploads_archive)
        return values, db_password

    def _check_upload_migration(self) -> None:
        existing = self.runner.get("deployment", f"{RELEASE}-librechat", self.namespace)
        if not existing:
            return
        spec = existing.get("spec", {}).get("template", {}).get("spec", {})
        mounted = any(
            mount.get("mountPath") == "/app/uploads" and mount.get("name") == "uploads"
            for container in spec.get("containers", [])
            for mount in container.get("volumeMounts", [])
        )
        claim = next(
            (
                volume.get("persistentVolumeClaim", {}).get("claimName")
                for volume in spec.get("volumes", [])
                if volume.get("name") == "uploads"
            ),
            None,
        )
        if mounted and claim == "librechat-uploads":
            if self.uploads_archive:
                raise StackError("Uploads already use the managed PVC; refusing archive overwrite")
            return
        if self.uploads_archive:
            return
        result = self.runner.kubectl(
            "exec",
            f"deployment/{RELEASE}-librechat",
            "-n",
            self.namespace,
            "--",
            "sh",
            "-c",
            "find /app/uploads -type f | wc -l",
        ).strip()
        if not result.isdigit():
            raise StackError("Cannot determine existing upload count")
        if int(result):
            raise StackError(
                "Existing uploads need migration; back up /app/uploads and pass "
                "--uploads-archive <tar>"
            )

    def _apply_secret(self, name: str, values: dict[str, str]) -> None:
        self.installation.component = OWNER
        self.installation.apply(
            resource(
                "Secret",
                name,
                self.namespace,
                type="Opaque",
                data={
                    key: base64.b64encode(value.encode()).decode() for key, value in values.items()
                },
            ),
            "addon",
        )

    def _restore_uploads(self) -> None:
        if not self.uploads_archive:
            return
        pod = resource(
            "Pod",
            "librechat-upload-restore",
            self.namespace,
            spec={
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "securityContext": {"runAsUser": 1000, "runAsGroup": 2000, "fsGroup": 2000},
                "containers": [
                    {
                        "name": "restore",
                        "image": "busybox:1.37.0",
                        "command": ["sh", "-c", "tail -f /dev/null"],
                        "volumeMounts": [{"name": "uploads", "mountPath": "/uploads"}],
                    }
                ],
                "volumes": [
                    {
                        "name": "uploads",
                        "persistentVolumeClaim": {"claimName": "librechat-uploads"},
                    }
                ],
            },
        )
        self.installation.apply(pod, "addon")
        try:
            self.runner.kubectl(
                "wait",
                "--for=condition=Ready",
                "pod/librechat-upload-restore",
                "-n",
                self.namespace,
                "--timeout=5m",
            )
            self.runner.run(
                [
                    "kubectl",
                    "--context",
                    self.runner.context,
                    "--request-timeout=30s",
                    "exec",
                    "-i",
                    "librechat-upload-restore",
                    "-n",
                    self.namespace,
                    "--",
                    "sh",
                    "-c",
                    "base64 -d | tar -xf - -C /uploads",
                ],
                data=b64encode(self.uploads_archive.read_bytes()).decode(),
            )
        except StackError:
            record = next(
                r
                for r in self.installation.state["resources"]
                if r["kind"] == "Pod" and r["name"] == "librechat-upload-restore"
            )
            self.installation.delete(record)
            raise
        record = next(
            r
            for r in self.installation.state["resources"]
            if r["kind"] == "Pod" and r["name"] == "librechat-upload-restore"
        )
        self.installation.delete(record)

    def _ensure_recreate_strategy(self) -> None:
        deployment = self.runner.get("deployment", f"{RELEASE}-librechat", self.namespace)
        if (
            deployment
            and deployment.get("spec", {}).get("strategy", {}).get("type") == "RollingUpdate"
        ):
            self.runner.kubectl(
                "patch",
                "deployment",
                f"{RELEASE}-librechat",
                "-n",
                self.namespace,
                "--type=merge",
                "-p",
                '{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null}}}',
            )

    def check(
        self, progress: Callable[[str], None] | None = None
    ) -> tuple[dict[str, str], str, list[str]]:
        values, db_password = self.preflight()
        acting = self.deployment.acting
        self.deployment.acting = [OWNER]
        try:
            warnings = check_prerequisites(self.deployment, progress=progress)
        finally:
            self.deployment.acting = acting
        self.installation.conflicts()
        self._check_upload_migration()
        return values, db_password, warnings

    def install(self, values: dict[str, str], db_password: str) -> None:
        local = self.config.secrets.addons.setdefault(NAME, {})
        local.update(
            {
                key: value
                for key, value in values.items()
                if key not in self.external_credentials
                and not (key == "ENKI_GATEWAY_API_KEY" and value == "sk-local-vllm")
            }
        )
        local["RAG_POSTGRES_PASSWORD"] = db_password
        self.config.save_secrets()
        state = self.installation.state["addons"].setdefault(NAME, {})
        state["status"] = "installing"
        self.installation.save()
        try:
            self.installation.bind(write=True, configuration=True)
            self.installation.component = OWNER
            if not self.runner.get("namespace", self.namespace):
                self.installation.apply(resource("Namespace", self.namespace), "namespaces")
            if self.config.profile.tls and self.config.profile.tls.mode == "self-signed":
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
            self._apply_secret(CHAT_SECRET, values)
            self._apply_secret(RAG_SECRET, {"POSTGRES_PASSWORD": db_password})
            route = self.deployment.resources["content"][-1]
            for doc in self.deployment.resources["content"][:-1]:
                self.installation.apply(doc, "addon")
            rag_docs = [
                doc
                for doc in self.deployment.resources["content"]
                if doc["kind"] in {"Deployment", "StatefulSet"}
            ]
            self.installation.wait_resources(rag_docs)
            self.runner.kubectl(
                "exec",
                "statefulset/librechat-rag-db",
                "-n",
                self.namespace,
                "--",
                "sh",
                "-c",
                'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" '
                '-d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SELECT 1"',
            )
            self.runner.kubectl(
                "rollout",
                "restart",
                "deployment/librechat-rag-api",
                "-n",
                self.namespace,
            )
            self.installation.wait_resources(rag_docs)
            self.runner.kubectl(
                "exec",
                "deployment/librechat-rag-api",
                "-n",
                self.namespace,
                "--",
                "python",
                "-c",
                "import json, math, os, urllib.request; "
                'r=urllib.request.Request(os.environ["RAG_OPENAI_BASEURL"].rstrip("/") + '
                '"/embeddings", data=json.dumps({"model":"embedding","input":"Readiness check",'
                '"encoding_format":"float"}).encode(), '
                'headers={"Content-Type":"application/json"}); '
                'v=json.load(urllib.request.urlopen(r, timeout=60))["data"][0]["embedding"]; '
                "assert len(v)==384 and all(math.isfinite(x) for x in v)",
            )
            self._restore_uploads()
            self._ensure_recreate_strategy()
            self.installation.helm_install(self.deployment.releases[0])
            self.installation.component = OWNER
            self.installation.apply(route, "addon")
            self.installation.wait_resources([route])
            state["status"] = "installed"
            self.installation.state["status"] = "installed"
            self.installation.component = None
            self.installation.capture_generated()
            self.installation.save()
        except StackError as exc:
            state["status"] = "failed"
            self.installation.component = None
            self.installation.save()
            raise StackError(
                f"LibreChat addon install failed; partial inventory retained at "
                f"{self.installation.path}. Correct the cause and rerun: {exc}"
            ) from None
