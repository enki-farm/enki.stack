from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets as random_secrets
import tarfile
from base64 import b64encode
from collections.abc import Callable
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from enkictl.checks import certificate
from enkictl.checks import preflight as check_prerequisites
from enkictl.config import Configuration, StackError
from enkictl.lifecycle import Installation
from enkictl.render import GW_NS, TLS_NAME, Deployment, Release, documents, resource
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
CODEAPI_RELEASE = "codeapi"
CODEAPI_CHART = (
    "https://github.com/LibreChat-AI/code-interpreter/releases/download/"
    "v1.10.13/codeapi-0.3.2.tgz"
)
CODEAPI_CHART_VERSION = "0.3.2"
CODEAPI_IMAGE_TAG = "v1.10.13-arm64"
CODEAPI_IMAGE_REGISTRY = "ghcr.io/enki-farm"
CODEAPI_IMAGES = (
    "code-interpreter-api",
    "code-interpreter-worker",
    "code-interpreter-sandbox-runner-direct",
    "code-interpreter-file-server",
    "code-interpreter-egress-gateway",
    "code-interpreter-package-init",
)
CHAT_SECRET = "librechat-credentials-env"
RAG_SECRET = "librechat-rag-db-credentials"
BUNDLER_SECRET = "librechat-bundler-oidc"
CODEAPI_SECRET = "codeapi-credentials"
CODEAPI_URL = "http://codeapi-api.librechat.svc.cluster.local:3112"
CODEAPI_JWT_KID = "enki-codeapi-ed25519-1"
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
    "CODEAPI_JWT_PRIVATE_KEY",
)
CODEAPI_SECRET_KEYS = (
    "CODEAPI_INTERNAL_SERVICE_TOKEN",
    "CODEAPI_EGRESS_GRANT_SECRET",
    "CODEAPI_REDIS_PASSWORD",
    "CODEAPI_MINIO_ROOT_PASSWORD",
    "CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY",
    "CODEAPI_EXECUTION_MANIFEST_PUBLIC_KEY",
    "CODEAPI_JWT_JWKS_JSON",
)


def _codeapi_jwks(private_key_pem: str) -> str:
    try:
        private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    except (TypeError, ValueError):
        raise StackError("Configured CodeAPI JWT private key is invalid") from None
    if not isinstance(private_key, Ed25519PrivateKey):
        raise StackError("Configured CodeAPI JWT private key must use Ed25519")
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return json.dumps(
        {
            "keys": [
                {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "x": base64.urlsafe_b64encode(public_key).rstrip(b"=").decode(),
                    "kid": CODEAPI_JWT_KID,
                    "alg": "EdDSA",
                }
            ]
        },
        separators=(",", ":"),
    )


def _codeapi_manifest_keys() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    private_der = private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return b64encode(private_der).decode(), b64encode(public_der).decode()


def _validate_codeapi_manifest_keys(private_value: str, public_value: str) -> None:
    try:
        private_key = serialization.load_der_private_key(
            base64.b64decode(private_value, validate=True), password=None
        )
        public_key = serialization.load_der_public_key(
            base64.b64decode(public_value, validate=True)
        )
    except (TypeError, ValueError):
        raise StackError("CodeAPI execution manifest signing keypair is invalid") from None
    if (
        not isinstance(private_key, Ed25519PrivateKey)
        or private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        != public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ):
        raise StackError("CodeAPI execution manifest signing keypair does not match")


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
        with_code_interpreter: bool = False,
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
        self.with_code_interpreter = with_code_interpreter or self.installation.state[
            "addons"
        ].get(NAME, {}).get("code_interpreter", False)
        self.external_credentials: set[str] = set()
        self._configure()

    def _configure(self) -> None:
        p = self.config.profile
        assert p.domain and p.tls
        rendered_rag = self.runner.render(self.root / "k8s/addons/librechat/rag")
        self.deployment.resources["content"] = documents(rendered_rag)
        for doc in self.deployment.resources["content"]:
            doc.setdefault("metadata", {}).setdefault("namespace", self.namespace)
            if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "librechat-rag-api":
                doc["spec"]["template"]["spec"]["containers"][0]["image"] = self.rag_image
        bundler = documents((self.root / "k8s/addons/librechat/bundler.yaml").read_text())
        config = next(doc for doc in bundler if doc["kind"] == "ConfigMap")
        workload = next(doc for doc in bundler if doc["kind"] == "Deployment")
        workload["spec"]["template"]["metadata"]["annotations"] = {
            "checksum/bundler-config": hashlib.sha256(
                yaml.safe_dump(config["data"]).encode()
            ).hexdigest()
        }
        bundler_routes = documents(
            (self.root / "k8s/addons/librechat/bundler-route.yaml")
            .read_text()
            .replace("zer0.garden", p.domain)
        )
        policy = next(doc for doc in bundler_routes if doc["kind"] == "SecurityPolicy")
        policy["spec"]["authorization"]["rules"][0]["principal"]["jwt"]["claims"][0]["values"] = [
            self.chat_group
        ]
        if p.tls.mode == "self-signed":
            bundler.append(
                resource(
                    "Backend",
                    "librechat-bundler-dex",
                    spec={
                        "endpoints": [{"fqdn": {"hostname": f"auth.{p.domain}", "port": 443}}],
                        "tls": {
                            "sni": f"auth.{p.domain}",
                            "caCertificateRefs": [
                                {"group": "", "kind": "ConfigMap", "name": "enki-platform-trust"}
                            ],
                        },
                    },
                )
            )
            refs = [
                {
                    "group": "gateway.envoyproxy.io",
                    "kind": "Backend",
                    "name": "librechat-bundler-dex",
                    "port": 443,
                }
            ]
            policy["spec"]["oidc"]["provider"]["backendRefs"] = refs
            policy["spec"]["jwt"]["providers"][0]["remoteJWKS"]["backendRefs"] = refs
        for doc in [*bundler, *bundler_routes]:
            doc["metadata"]["namespace"] = self.namespace
        self.deployment.resources["content"].extend(bundler)
        self.deployment.resources["bundler-routes"] = bundler_routes
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
                "SANDPACK_BUNDLER_URL": p.origin("bundler"),
                "SANDPACK_STATIC_BUNDLER_URL": f"https://preview.bundler.{p.domain}",
                "CSP_FRAME_SRC_EXTRA": f"{p.origin('bundler')} https://*.bundler.{p.domain}",
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
        if self.with_code_interpreter:
            values["librechat"]["configEnv"].update(
                {
                    "LIBRECHAT_CODE_BASEURL": (
                        f"http://{CODEAPI_RELEASE}-api.{self.namespace}.svc.cluster.local:3112"
                    ),
                    "CODEAPI_AUTH_PROVIDER": "librechat-jwt",
                    "CODEAPI_JWT_ALGORITHM": "EdDSA",
                    "CODEAPI_JWT_KID": CODEAPI_JWT_KID,
                    "CODEAPI_JWT_ISSUER": "librechat",
                    "CODEAPI_JWT_AUDIENCE": "codeapi",
                    "CODEAPI_JWT_TTL_SECONDS": "300",
                    "CODEAPI_JWT_MINT_CACHE_SECONDS": "30",
                    "CODEAPI_JWT_SINGLE_TENANT_ID": "legacy",
                }
            )
            codeapi_values = yaml.safe_load(
                (self.root / "k8s/addons/librechat/codeapi-values.yaml").read_text()
            )
            storage_class = p.storage_class
            codeapi_values["global"]["storageClass"] = storage_class
            codeapi_values["workerSandbox"]["packages"]["persistence"][
                "storageClass"
            ] = storage_class
            codeapi_values["redis"]["master"]["persistence"]["storageClass"] = storage_class
            codeapi_values["minio"]["persistence"]["storageClass"] = storage_class
            self.deployment.overrides[CODEAPI_RELEASE] = codeapi_values
            seccomp_check = documents(
                (self.root / "k8s/addons/librechat/codeapi-seccomp-check.yaml").read_text()
            )
            for doc in seccomp_check:
                doc["metadata"]["namespace"] = self.namespace
            self.deployment.resources["content"].extend(seccomp_check)
        config = yaml.safe_load(values["librechat"]["configYamlContent"])
        config.setdefault("registration", {})["socialLogins"] = ["openid"]
        if self.with_code_interpreter:
            config.setdefault("interface", {})["runCode"] = True
        values["librechat"]["configYamlContent"] = yaml.safe_dump(config, sort_keys=False)
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
        self.deployment.releases = (
            [
                Release(
                    name=CODEAPI_RELEASE,
                    namespace=self.namespace,
                    chart=CODEAPI_CHART,
                    version=CODEAPI_CHART_VERSION,
                    phase="addon",
                    values=[],
                    component=OWNER,
                ),
                release,
            ]
            if self.with_code_interpreter
            else [release]
        )

    def _credentials(self) -> tuple[dict[str, str], str]:
        existing_chat = _decode_secret(self.runner.get("secret", CHAT_SECRET, self.namespace))
        existing_codeapi = (
            _decode_secret(self.runner.get("secret", CODEAPI_SECRET, self.namespace))
            if self.with_code_interpreter
            else {}
        )
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
            if key == "CODEAPI_JWT_PRIVATE_KEY" and not self.with_code_interpreter:
                continue
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
            if not value and key == "CODEAPI_JWT_PRIVATE_KEY":
                private_key = Ed25519PrivateKey.generate()
                value = private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                ).decode()
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

        if self.with_code_interpreter:
            codeapi_values = {}
            token_keys = (
                "CODEAPI_INTERNAL_SERVICE_TOKEN",
                "CODEAPI_EGRESS_GRANT_SECRET",
                "CODEAPI_REDIS_PASSWORD",
                "CODEAPI_MINIO_ROOT_PASSWORD",
            )
            for key in token_keys:
                requested = local.get(key, "")
                stored = existing_codeapi.get(key, "")
                if requested and stored and requested != stored:
                    raise StackError(
                        f"Configured CodeAPI credential differs from {CODEAPI_SECRET}:{key}"
                    )
                codeapi_values[key] = requested or stored or random_secrets.token_hex(32)

            private_name = "CODEAPI_EXECUTION_MANIFEST_PRIVATE_KEY"
            public_name = "CODEAPI_EXECUTION_MANIFEST_PUBLIC_KEY"
            manifest_private = local.get(private_name) or existing_codeapi.get(private_name, "")
            manifest_public = local.get(public_name) or existing_codeapi.get(public_name, "")
            if bool(manifest_private) != bool(manifest_public):
                raise StackError("CodeAPI execution manifest signing keypair is incomplete")
            if not manifest_private:
                manifest_private, manifest_public = _codeapi_manifest_keys()
            for key, value in ((private_name, manifest_private), (public_name, manifest_public)):
                requested = local.get(key, "")
                stored = existing_codeapi.get(key, "")
                if (requested and requested != value) or (stored and stored != value):
                    raise StackError(
                        f"Configured CodeAPI credential differs from {CODEAPI_SECRET}:{key}"
                    )
                codeapi_values[key] = value
            _validate_codeapi_manifest_keys(manifest_private, manifest_public)

            jwks = _codeapi_jwks(values["CODEAPI_JWT_PRIVATE_KEY"])
            jwks_sources = (
                local.get("CODEAPI_JWT_JWKS_JSON", ""),
                existing_codeapi.get("CODEAPI_JWT_JWKS_JSON", ""),
            )
            for source in jwks_sources:
                if source and source != jwks:
                    raise StackError("CodeAPI JWT JWKS does not match LibreChat's signing key")
            codeapi_values["CODEAPI_JWT_JWKS_JSON"] = jwks
            values.update(codeapi_values)
            chart_values = self.deployment.overrides[CODEAPI_RELEASE]
            chart_values["internalServiceAuth"]["token"] = codeapi_values[
                "CODEAPI_INTERNAL_SERVICE_TOKEN"
            ]
            chart_values["egressGrant"]["secret"] = codeapi_values[
                "CODEAPI_EGRESS_GRANT_SECRET"
            ]
            chart_values["executionManifest"].update(
                {"privateKey": manifest_private, "publicKey": manifest_public}
            )
            chart_values["redis"]["auth"]["password"] = codeapi_values[
                "CODEAPI_REDIS_PASSWORD"
            ]
            chart_values["minio"]["auth"]["rootPassword"] = codeapi_values[
                "CODEAPI_MINIO_ROOT_PASSWORD"
            ]

        bundler_secret = dex_values.get("BUNDLER_CLIENT_SECRET")
        if not bundler_secret:
            raise StackError(
                "Dex bundler client is not configured; run enkictl component install auth "
                "with this profile first"
            )
        stored_bundler = _decode_secret(
            self.runner.get("secret", BUNDLER_SECRET, self.namespace)
        ).get("client-secret")
        if stored_bundler and stored_bundler != bundler_secret:
            raise StackError(
                "Bundler OIDC client credential differs from Dex; restore matching credentials "
                "before reinstalling"
            )
        self.deployment.resources["credentials"] = [
            resource(
                "Secret",
                BUNDLER_SECRET,
                self.namespace,
                type="Opaque",
                data={"client-secret": b64encode(bundler_secret.encode()).decode()},
            )
        ]
        if self.with_code_interpreter:
            self.deployment.resources["credentials"].append(
                resource(
                    "Secret",
                    CODEAPI_SECRET,
                    self.namespace,
                    type="Opaque",
                    data={
                        key: b64encode(values[key].encode()).decode()
                        for key in CODEAPI_SECRET_KEYS
                    },
                )
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
        for name, title, host, callback in (
            ("librechat", "LibreChat", "chat", "/oauth/openid/callback"),
            ("bundler", "bundler", "bundler", "/oauth2/callback"),
        ):
            if not any(
                client.get("id") == name
                and client.get("secretEnv") == f"{name.upper()}_CLIENT_SECRET"
                and self.config.profile.origin(host) + callback in client.get("redirectURIs", [])
                for client in clients
            ):
                raise StackError(
                    f"Dex {title} callback is not registered; run enkictl component install auth "
                    "with this profile first"
                )
        self._check_bundler_tls()
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
        if self.with_code_interpreter:
            self._check_codeapi_images()
        values, db_password = self._credentials()
        if self.uploads_archive:
            _validate_archive(self.uploads_archive)
        return values, db_password

    def _check_codeapi_images(self) -> None:
        for image in CODEAPI_IMAGES:
            reference = f"{CODEAPI_IMAGE_REGISTRY}/{image}:{CODEAPI_IMAGE_TAG}"
            try:
                output = self.runner.run(
                    ["docker", "buildx", "imagetools", "inspect", reference]
                )
            except StackError:
                raise StackError(
                    f"Could not inspect CodeAPI image {reference}; log in to GHCR and run "
                    "bash scripts/build-code-interpreter-images.sh"
                ) from None
            if "linux/arm64" not in output:
                raise StackError(
                    f"CodeAPI image is not available for linux/arm64: {reference}; "
                    "run bash scripts/build-code-interpreter-images.sh"
                )

    def _check_bundler_tls(self) -> None:
        domain = self.config.profile.domain
        assert domain
        secret = self.runner.get("secret", TLS_NAME, GW_NS)
        if not secret:
            raise StackError("Platform TLS Secret is missing; install certificates first")
        cert = x509.load_pem_x509_certificate(certificate(secret, domain).encode())
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if f"*.bundler.{domain}" not in names.get_values_for_type(x509.DNSName):
            raise StackError(
                "Platform certificate lacks *.bundler.<domain> coverage; run "
                "enkictl component install certificates with this profile first"
            )

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
        warnings.append(
            f"Artifacts require DNS for bundler.{self.config.profile.domain} and "
            f"*.bundler.{self.config.profile.domain}. Open "
            f"{self.config.profile.origin('bundler')} in a top-level tab to log in before "
            "using previews. Enable Artifacts in each agent's tools."
        )
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
        state["code_interpreter"] = self.with_code_interpreter
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
            self._apply_secret(
                CHAT_SECRET, {key: value for key, value in values.items() if key in SECRET_KEYS}
            )
            self._apply_secret(RAG_SECRET, {"POSTGRES_PASSWORD": db_password})
            credentials = self.deployment.resources.get("credentials", [])
            if not credentials:
                raise StackError("Bundler credentials are missing; run LibreChat preflight first")
            for doc in credentials:
                self.installation.apply(doc, "addon")
            route = self.deployment.resources["content"][-1]
            for doc in self.deployment.resources["content"][:-1]:
                self.installation.apply(doc, "addon")
            workloads = [
                doc
                for doc in self.deployment.resources["content"]
                if doc["kind"] in {"Deployment", "StatefulSet", "DaemonSet"}
            ]
            self.installation.wait_resources(workloads)
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
            self.installation.wait_resources(workloads)
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
            bundler_routes = self.deployment.resources["bundler-routes"]
            for doc in bundler_routes:
                self.installation.apply(doc, "addon")
            self.installation.wait_resources(bundler_routes)
            for release in self.deployment.releases:
                self.installation.helm_install(release)
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
