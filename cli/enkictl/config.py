from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StackError(Exception):
    """An actionable configuration or deployment failure."""


class KubernetesLoader(yaml.SafeLoader):
    """Accept Kubernetes' literal '=' enum values without enabling object tags."""


KubernetesLoader.add_constructor("tag:yaml.org,2002:value", yaml.SafeLoader.construct_scalar)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def domain_name(value: str) -> str:
    if (
        len(value) > 253
        or "." not in value
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in value.split(".")
        )
    ):
        raise ValueError("must be a lowercase DNS domain without scheme, port, wildcard or path")
    return value


class Azure(StrictModel):
    subscription_id: UUID
    tenant_id: UUID
    client_id: UUID
    resource_group: Annotated[str, Field(min_length=1, max_length=90)]


class SelfSignedTLS(StrictModel):
    mode: Literal["self-signed"]


class ACMETLS(StrictModel):
    mode: Literal["azure", "cloudflare"]
    acme_email: str
    environment: Literal["production", "staging"] = "production"
    dns_zone: str
    azure: Azure | None = None

    _zone = field_validator("dns_zone")(domain_name)

    @field_validator("acme_email")
    @classmethod
    def email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("must be an email address")
        return value

    @model_validator(mode="after")
    def solver(self) -> Self:
        if (self.mode == "azure") != (self.azure is not None):
            raise ValueError("azure settings are required only for the Azure solver")
        return self

    @property
    def server(self) -> str:
        host = "acme-v02" if self.environment == "production" else "acme-staging-v02"
        return f"https://{host}.api.letsencrypt.org/directory"


TLS = Annotated[SelfSignedTLS | ACMETLS, Field(discriminator="mode")]


class Auth(StrictModel):
    connector: str = "connector.yaml"
    hub_group: str = ""
    chat_group: str = ""
    grafana_viewer_group: str = ""
    grafana_admin_group: str = ""

    @field_validator("hub_group", "chat_group", "grafana_viewer_group", "grafana_admin_group")
    @classmethod
    def group(cls, value: str, info) -> str:
        if not value:
            return value
        if not re.fullmatch(r"[A-Za-z0-9_.:@/ -]+", value):
            raise ValueError("group is required; use letters, digits, spaces and _.:@/-")
        return value


class Profile(StrictModel):
    schema_version: Literal[1] = 1
    platform: Literal["dgx-spark", "aks"]
    context: Annotated[str, Field(min_length=1)]
    domain: str | None = None
    gpu: Literal["operator", "existing"] = "existing"
    auth: Auth | None = None
    tls: TLS | None = None

    @field_validator("domain")
    @classmethod
    def valid_domain(cls, value: str | None) -> str | None:
        return domain_name(value) if value is not None else None

    @model_validator(mode="after")
    def compatible(self) -> Self:
        if self.domain:
            domain_name(f"grafana.{self.domain}")
        if self.platform == "aks" and self.gpu != "existing":
            raise ValueError("AKS requires existing GPU management")
        if isinstance(self.tls, ACMETLS) and self.domain:
            if not (
                self.domain == self.tls.dns_zone or self.domain.endswith("." + self.tls.dns_zone)
            ):
                raise ValueError("DNS zone must equal or be an authoritative parent of the domain")
        return self

    def origin(self, service: str) -> str:
        if not self.domain:
            raise StackError("This component requires a domain")
        return f"https://{service}.{self.domain}"

    @property
    def storage_class(self) -> str:
        return "local-path" if self.platform == "dgx-spark" else "managed-csi"


class Secrets(StrictModel):
    connector: dict[str, str] = Field(default_factory=dict)
    cloudflare_api_token: str | None = None
    azure_client_secret: str | None = None
    generated: dict[str, str] = Field(default_factory=dict)
    addons: dict[str, dict[str, str]] = Field(default_factory=dict)


def read_yaml(path: Path) -> object:
    try:
        with path.open() as stream:
            return yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        # YAML parser exceptions can include source lines containing credentials.
        raise StackError(f"Cannot read valid YAML from {path} ({type(exc).__name__})") from None


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def dump(value: object) -> str:
    return yaml.safe_dump(value, sort_keys=False)


REFERENCE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def validate_connector(connector: object) -> None:
    if (
        not isinstance(connector, dict)
        or set(connector) != {"connectors"}
        or not isinstance(connector["connectors"], list)
        or len(connector["connectors"]) != 1
    ):
        raise StackError("Connector YAML must contain exactly one entry in a connectors list")
    item = connector["connectors"][0]
    if (
        not isinstance(item, dict)
        or not all(item.get(key) for key in ("id", "type", "config"))
        or not isinstance(item["config"], dict)
    ):
        raise StackError("Dex connector requires id, type and a config mapping")

    def inspect(value: object, key: str = "") -> None:
        if isinstance(value, dict):
            for name, child in value.items():
                inspect(child, str(name))
        elif isinstance(value, list):
            for child in value:
                inspect(child, key)
        elif isinstance(value, str) and key.lower() in {
            "clientsecret",
            "password",
            "bindpw",
            "token",
            "privatekey",
            "secretaccesskey",
            "serviceaccountjson",
        }:
            if not REFERENCE.fullmatch(value):
                raise StackError(f"Connector {key} must reference a secret as $NAME")

    inspect(connector)


class Configuration:
    def __init__(self, path: Path, load_connector: bool = True, targets: list[str] | None = None):
        from enkictl.components import defaults, resolve

        self.path = path.resolve()
        self.profile = Profile.model_validate(read_yaml(self.path))
        self.components = resolve(targets if targets is not None else defaults(self.profile.gpu))
        self.secret_path = self.path.parent / "secrets.yaml"
        if self.secret_path.exists() and self.secret_path.stat().st_mode & 0o077:
            raise StackError(f"Restrict secret permissions first: chmod 600 {self.secret_path}")
        try:
            self.secrets = Secrets.model_validate(
                read_yaml(self.secret_path) if self.secret_path.exists() else {}
            )
        except ValueError:
            raise StackError(
                f"Invalid secrets schema in {self.secret_path}; values withheld"
            ) from None
        self.connector: dict = {}
        self.references: set[str] = set()
        if load_connector:
            self.configure(self.components)

    def configure(self, components: list[str]) -> None:
        self.components = components
        self.connector = {}
        self.references = set()
        if "auth" in components:
            if not self.profile.auth:
                raise StackError("auth requires an auth configuration and connector")
            connector_path = (self.path.parent / self.profile.auth.connector).resolve()
            connector = read_yaml(connector_path)
            validate_connector(connector)
            self.connector = connector
            self.references = set(REFERENCE.findall(dump(connector)))

    def validate(self) -> None:
        p = self.profile
        if any(name != "gpu-operator" for name in self.components):
            if not p.domain or not p.tls:
                raise StackError("Selected components require domain and tls configuration")
        if "gpu-operator" in self.components and (p.platform != "dgx-spark" or p.gpu != "operator"):
            raise StackError("gpu-operator requires dgx-spark with gpu: operator")
        for component, field in (
            ("monitoring", "grafana_viewer_group"),
            ("kubeflow-hub", "hub_group"),
        ):
            if component in self.components and (not p.auth or not getattr(p.auth, field)):
                raise StackError(f"{component} requires auth.{field}")
        missing = sorted(
            key for key in self.references if not self.secrets.connector.get(key, "").strip()
        )
        if missing:
            raise StackError("Missing connector secrets: " + ", ".join(missing))
        reserved = {"GRAFANA_CLIENT_SECRET", "HUB_CLIENT_SECRET"}
        if self.references & reserved:
            raise StackError("Connector references reserved application credential names")
        tls = self.profile.tls
        if "certificates" in self.components and isinstance(tls, ACMETLS):
            key = "azure_client_secret" if tls.mode == "azure" else "cloudflare_api_token"
            if not getattr(self.secrets, key):
                raise StackError(f"Missing {key} in {self.secret_path}")
        values = [
            self.profile.context,
            *(self.secrets.connector[key] for key in self.references),
        ]
        if "certificates" in self.components and isinstance(tls, ACMETLS):
            values.extend([tls.acme_email, getattr(self.secrets, key)])
        if any(value and ("REPLACE_ME" in value or "CHANGE_ME" in value) for value in values):
            raise StackError("Replace example placeholders before deployment")
        if (
            any(name != "gpu-operator" for name in self.components)
            and self.profile.domain
            and (
                self.profile.domain == "example.com" or self.profile.domain.endswith(".example.com")
            )
        ):
            raise StackError("Replace the example domain before deployment")

    def save_secrets(self) -> None:
        private_write(self.secret_path, dump(self.secrets.model_dump(exclude_none=True)))


def stack_root(explicit: Path | None, components: list[str] | None = None) -> Path:
    from enkictl.components import REGISTRY

    if explicit:
        root = explicit.resolve()
    else:
        root = next(
            (p for p in (Path.cwd(), *Path.cwd().parents) if (p / "helm").is_dir()),
            Path(__file__).resolve().parents[2],
        )
    required = (
        [asset for name in components for asset in REGISTRY[name].assets]
        if components is not None
        else ["helm/gateway", "k8s/dex", "k8s/kubeflow-hub/upstream"]
    )
    for relative in required:
        if not (root / relative).is_dir():
            raise StackError(
                f"Missing stack assets: {root / relative}; use --stack-root or vendor Hub"
            )
    return root
