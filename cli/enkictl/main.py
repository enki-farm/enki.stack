from __future__ import annotations

import functools
import sys
from pathlib import Path

import questionary
import typer
from pydantic import ValidationError

from enkictl.addons import REGISTRY as ADDONS
from enkictl.checks import preflight as check_prerequisites
from enkictl.components import REGISTRY, defaults, phases, resolve
from enkictl.config import (
    REFERENCE,
    Configuration,
    Profile,
    Secrets,
    StackError,
    dump,
    private_write,
    read_yaml,
    stack_root,
    validate_connector,
)
from enkictl.lifecycle import Installation
from enkictl.render import Deployment
from enkictl.tools import Runner

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Configure and deploy enki.stack with Helm and Kustomize.",
)
addon_app = typer.Typer(no_args_is_help=True, help="Install and remove optional addons.")
app.add_typer(addon_app, name="addon")
component_app = typer.Typer(no_args_is_help=True, help="List installed components.")
app.add_typer(component_app, name="component")
DEFAULT = Path("profiles/local/profile.yaml")


def addon_order(names: list[str]) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name not in ADDONS:
            raise StackError(f"Unknown addon: {name}; choose from {', '.join(ADDONS)}")
        if name in visiting:
            raise StackError(f"Addon dependency cycle at {name}")
        if name in ordered:
            return
        visiting.add(name)
        for dependency in getattr(ADDONS[name], "addon_dependencies", ()):
            visit(dependency)
        visiting.remove(name)
        ordered.append(name)

    for name in names:
        visit(name)
    return ordered


def guarded(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValidationError as exc:
            for error in exc.errors(include_input=False, include_url=False):
                typer.echo(f"Error: {'.'.join(map(str, error['loc']))}: {error['msg']}", err=True)
            raise typer.Exit(1) from None
        except StackError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from None
        except (OSError, ValueError, KeyError) as exc:
            typer.echo(
                f"Error: {type(exc).__name__}; invalid local state or tool response "
                "(details withheld to protect credentials)",
                err=True,
            )
            raise typer.Exit(1) from None

    return wrapped


def prompt(label: str, default: str = "", secret: bool = False) -> str:
    if sys.stdin.isatty():
        answer = (
            questionary.password(label, default=default).ask()
            if secret
            else questionary.text(label, default=default).ask()
        )
        if answer is None:
            raise typer.Abort()
        return answer
    return typer.prompt(
        label,
        default=default,
        hide_input=secret,
        show_default=bool(default) and not secret,
    )


def prompt_choice(label: str, choices: list[str], default: str) -> str:
    if sys.stdin.isatty():
        answer = questionary.select(label, choices=choices, default=default).ask()
        if answer is None:
            raise typer.Abort()
        return answer
    answer = typer.prompt(f"{label} ({'/'.join(choices)})", default=default)
    if answer not in choices:
        raise StackError(f"Choose one of: {', '.join(choices)}")
    return answer


def prompt_confirm(label: str, default: bool = False) -> bool:
    if sys.stdin.isatty():
        answer = questionary.confirm(label, default=default).ask()
        if answer is None:
            raise typer.Abort()
        return answer
    return typer.confirm(label, default=default)


def prompt_selection(label: str, available: list[str], selected: list[str]) -> list[str]:
    if sys.stdin.isatty():
        checkbox_prompt = questionary.checkbox(
            label,
            choices=[
                questionary.Choice(name, value=name, checked=name in selected) for name in available
            ],
            instruction="Use arrow keys to move, <space> to select, <i> to invert",
        )
        checkbox_prompt.application.key_bindings.remove("a")
        answer = checkbox_prompt.ask()
        if answer is None:
            raise typer.Abort()
        return answer
    default = ", ".join(selected) or "none"
    value = prompt(f"{label} (comma-separated: {', '.join(available)}; or none)", default)
    if value.strip().lower() == "none":
        return []
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def local_inventory(profile: Path, root: Path | None) -> Installation:
    config = Configuration(profile, load_connector=False, targets=[])
    deployment = Deployment(
        config, stack_root(root, []), Runner(config.profile.context), targets=[]
    )
    return Installation(deployment)


@app.command("init")
@guarded
def initialize(
    targets: list[str] | None = typer.Option(
        None, "--component", help="Component to select; repeat to select multiple."
    ),
    profile: Path = typer.Option(
        DEFAULT, "--profile", "-p", help="Profile YAML to create or revise."
    ),
) -> None:
    """Collect only relevant settings, then review and revise before saving."""
    profile = profile.resolve()
    old = read_yaml(profile) if profile.exists() else {}
    if old and not prompt_confirm(f"Revise existing profile {profile}?"):
        raise typer.Abort()
    secret_path = profile.parent / "secrets.yaml"
    secret_values = Secrets.model_validate(read_yaml(secret_path) if secret_path.exists() else {})
    inventory_path = profile.parent / "inventory.yaml"
    inventory = read_yaml(inventory_path) if inventory_path.exists() else {}
    if not isinstance(inventory, dict):
        raise StackError(f"Invalid installation inventory at {inventory_path}")
    if inventory and inventory.get("schema_version") != 2:
        raise StackError(f"Unsupported inventory schema at {inventory_path}")
    desired = inventory.get("desired", {})
    if not isinstance(desired, dict):
        raise StackError(f"Invalid desired selection in {inventory_path}")
    for key in ("components", "addons"):
        if key in desired and (
            not isinstance(desired[key], list)
            or any(not isinstance(item, str) for item in desired[key])
        ):
            raise StackError(f"Invalid desired {key} selection in {inventory_path}")
    draft = dict(old or {})
    connector_text = ""
    selected_components = (
        list(targets)
        if targets is not None
        else list(desired["components"])
        if "components" in desired
        else defaults(draft.get("gpu", "operator"))
    )
    component_selection_is_explicit = targets is not None or "components" in desired
    selected_addons = list(desired.get("addons", []))
    needed = resolve(selected_components)
    section = "all"
    while True:
        if section in {"all", "platform"}:
            platform = prompt_choice(
                "Platform", ["dgx-spark", "aks"], draft.get("platform", "dgx-spark")
            )
            current = draft.get("context", "")
            if not current:
                # Init remains usable without kubectl or a reachable API.
                try:
                    current = Runner("").run(["kubectl", "config", "current-context"]).strip()
                except StackError:
                    typer.echo("No local current kubecontext available; enter its name explicitly.")
            draft.update(
                schema_version=1,
                platform=platform,
                context=prompt("Kubecontext", current),
            )
            draft.update(
                gpu="existing"
                if platform == "aks"
                else prompt_choice(
                    "GPU management", ["operator", "existing"], draft.get("gpu", "operator")
                ),
            )
            if section == "all" and not component_selection_is_explicit:
                selected_components = [
                    name
                    for name in REGISTRY
                    if name != "gpu-operator" or draft["gpu"] == "operator"
                ]
                needed = resolve(selected_components)
        if section in {"all", "selection"}:
            try:
                selected_components = prompt_selection(
                    "Components to install", list(REGISTRY), selected_components
                )
                component_selection_is_explicit = True
                needed = resolve(selected_components)
                selected_addons = prompt_selection(
                    "Addons to install", list(ADDONS), selected_addons
                )
                unknown_addons = set(selected_addons) - set(ADDONS)
                if unknown_addons:
                    raise StackError(
                        f"Unknown addon: {', '.join(sorted(unknown_addons))}; "
                        f"choose from {', '.join(ADDONS)}"
                    )
                if "librechat" in selected_addons:
                    needed = resolve([*selected_components, "auth"])
            except StackError as exc:
                typer.echo(f"Error: {exc}")
                section = "selection"
                continue
        if section in {"all", "platform", "selection"} and (
            selected_addons or any(name != "gpu-operator" for name in needed)
        ):
            draft["domain"] = prompt("Base domain", draft.get("domain", ""))
        if section in {"all", "tls"} and "certificates" in needed:
            previous = draft.get("tls") or {}
            mode = prompt_choice(
                "Certificate mode",
                ["azure", "cloudflare", "self-signed"],
                previous.get("mode", "cloudflare"),
            )
            tls = {"mode": mode}
            if mode in {"azure", "cloudflare"}:
                tls.update(
                    acme_email=prompt("ACME registration email", previous.get("acme_email", "")),
                    environment=prompt(
                        "ACME environment (production/staging)",
                        previous.get("environment", "production"),
                    ),
                    dns_zone=prompt(
                        "Authoritative DNS zone", previous.get("dns_zone", draft["domain"])
                    ),
                )
                if mode == "azure":
                    azure = previous.get("azure", {})
                    tls["azure"] = {
                        key: prompt(label, azure.get(key, ""))
                        for key, label in (
                            ("subscription_id", "Azure subscription ID"),
                            ("tenant_id", "Azure tenant ID"),
                            ("client_id", "Service principal client ID"),
                            ("resource_group", "DNS zone resource group"),
                        )
                    }
                    secret_values.azure_client_secret = prompt(
                        "Azure client secret", secret_values.azure_client_secret or "", True
                    )
                    typer.echo("Grant this principal DNS Zone Contributor on this DNS zone only.")
                else:
                    secret_values.cloudflare_api_token = prompt(
                        "Cloudflare API token", secret_values.cloudflare_api_token or "", True
                    )
                    typer.echo("Token needs Zone DNS Edit and Zone Read scoped to this zone.")
            elif mode == "self-signed":
                typer.echo(
                    "Self-signed: manually trust the exported public certificate on browsers, "
                    "external machines and nodes. Managed components receive trust automatically."
                )
            draft["tls"] = tls
        if section in {"all", "auth"} and "auth" in needed:
            auth = draft.get("auth") or {}
            existing_connector = profile.parent / auth.get("connector", "connector.yaml")
            source = prompt_choice(
                "Dex connector source",
                ["github", "entra", "file"],
                "file" if existing_connector.exists() else "github",
            )
            if source == "file":
                connector_path = (
                    Path(prompt("Dex connector YAML file", str(existing_connector)))
                    .expanduser()
                    .resolve()
                )
                connector = read_yaml(connector_path)
            elif source == "github":
                org = prompt("GitHub organization")
                teams = prompt("GitHub allowed team slugs (comma-separated)", "core,ai-users")
                connector = {
                    "connectors": [
                        {
                            "type": "github",
                            "id": "github",
                            "name": "GitHub",
                            "config": {
                                "clientID": "$GITHUB_CLIENT_ID",
                                "clientSecret": "$GITHUB_CLIENT_SECRET",
                                "redirectURI": "@DEX_ISSUER@/callback",
                                "teamNameField": "slug",
                                "orgs": [
                                    {"name": org, "teams": [t.strip() for t in teams.split(",")]}
                                ],
                            },
                        }
                    ]
                }
            elif source == "entra":
                tenant = prompt("Entra tenant ID")
                groups = prompt("Entra allowed group object IDs (comma-separated)")
                connector = {
                    "connectors": [
                        {
                            "type": "microsoft",
                            "id": "entra",
                            "name": "Microsoft Entra ID",
                            "config": {
                                "clientID": "$ENTRA_CLIENT_ID",
                                "clientSecret": "$ENTRA_CLIENT_SECRET",
                                "redirectURI": "@DEX_ISSUER@/callback",
                                "tenant": tenant,
                                "groupNameFormat": "id",
                                "useGroupsAsWhitelist": True,
                                "groups": [g.strip() for g in groups.split(",")],
                            },
                        }
                    ]
                }
            else:
                raise StackError("Choose github, entra or file for the Dex connector")
            validate_connector(connector)
            connector_text = dump(connector)
            for key in sorted(set(REFERENCE.findall(connector_text))):
                secret_values.connector[key] = prompt(
                    f"Connector value {key}", secret_values.connector.get(key, ""), True
                )
            draft["auth"] = {
                "connector": "connector.yaml",
                "hub_group": prompt("Hub access group", auth.get("hub_group", ""))
                if "kubeflow-hub" in needed
                else auth.get("hub_group", ""),
                "chat_group": prompt(
                    "LibreChat access group (empty uses Hub group)", auth.get("chat_group", "")
                )
                if "librechat" in selected_addons
                else auth.get("chat_group", ""),
                "grafana_viewer_group": prompt(
                    "Grafana viewer group", auth.get("grafana_viewer_group", "")
                )
                if "monitoring" in needed
                else auth.get("grafana_viewer_group", ""),
                "grafana_admin_group": prompt(
                    "Grafana admin group (empty grants nobody Admin)",
                    default=auth.get("grafana_admin_group", ""),
                )
                if "monitoring" in needed
                else auth.get("grafana_admin_group", ""),
            }
        try:
            validated = Profile.model_validate(draft)
            if (
                targets
                and "gpu-operator" in needed
                and (validated.platform != "dgx-spark" or validated.gpu != "operator")
            ):
                raise StackError("gpu-operator requires dgx-spark with gpu: operator")
        except ValidationError as exc:
            for error in exc.errors(include_input=False, include_url=False):
                typer.echo(f"Invalid {'.'.join(map(str, error['loc']))}: {error['msg']}")
            section = prompt_choice("Revise section", ["platform", "tls", "auth"], "platform")
            continue
        typer.echo("\nConfiguration review (secret values hidden):")
        typer.echo(dump(validated.model_dump(mode="json", exclude_none=True)))
        typer.echo(
            "Secrets: connector values and selected solver credentials stored in secrets.yaml."
        )
        action = prompt_choice(
            "Next action", ["save", "platform", "selection", "tls", "auth", "cancel"], "save"
        )
        if action == "cancel":
            raise typer.Abort()
        if action != "save":
            if action not in {"platform", "selection", "tls", "auth"}:
                typer.echo("Choose save, platform, selection, tls, auth or cancel.")
                continue
            section = action
            continue
        if "auth" in needed and not connector_text:
            connector_text = (profile.parent / draft["auth"]["connector"]).read_text()
        # Commit local secret material first, then the non-secret profile.
        private_write(secret_path, dump(secret_values.model_dump(exclude_none=True)))
        if connector_text:
            private_write(profile.parent / "connector.yaml", connector_text)
        private_write(profile, dump(validated.model_dump(mode="json", exclude_none=True)))
        config = Configuration(profile, load_connector=False, targets=[])
        deployment = Deployment(
            config, stack_root(None, []), Runner(config.profile.context), targets=[]
        )
        installation = Installation(deployment)
        installation.state["desired"] = {
            "components": selected_components,
            "addons": selected_addons,
        }
        installation.save()
        typer.echo(f"Saved {profile}. Secrets are local plaintext; do not commit secrets.yaml.")
        typer.echo(f"Saved installation selections to {installation.path}.")
        return


def prepare(
    profile: Path,
    root: Path | None,
    charts: bool = True,
    targets: list[str] | None = None,
    compose: bool = True,
) -> tuple[Deployment, Installation]:
    if targets:
        resolve(targets)
    config = Configuration(profile, load_connector=False, targets=targets)
    deployment = Deployment(
        config, stack_root(root, []), Runner(config.profile.context), targets=targets
    )
    installation = Installation(deployment)
    installation.plan()
    config.configure(deployment.acting)
    config.validate()
    stack_root(root, deployment.acting)
    if compose:
        deployment.compose()
    if charts:
        deployment.render_charts()
    return deployment, installation


@app.command()
@guarded
def validate(
    targets: list[str] | None = typer.Argument(None, help="Components to validate; default: core."),
    profile: Path = typer.Option(DEFAULT, "--profile", "-p"),
    render: bool = typer.Option(
        False, help="Also render local Kustomize assets and remote Helm charts."
    ),
    root: Path | None = typer.Option(None, "--stack-root"),
) -> None:
    """Validate configuration without accessing the Kubernetes API."""
    prepare(profile, root, charts=render, targets=targets, compose=render)
    typer.echo("Configuration valid." + (" Helm/Kustomize rendering passed." if render else ""))


def preview(deployment: Deployment) -> None:
    typer.echo(
        f"Target: {deployment.profile.context}, platform: {deployment.profile.platform}, "
        f"domain: {deployment.profile.domain}"
    )
    typer.echo("Requested: " + ", ".join(deployment.requested))
    for name in deployment.components:
        action = "reconcile/install" if name in deployment.acting else "verify installed dependency"
        origin = "requested" if name in deployment.requested else "dependency"
        typer.echo(f"  {name}: {action} ({origin})")
    for phase in phases(deployment.acting):
        releases = [r.name for r in deployment.releases if r.phase == phase]
        typer.echo(
            f"  {phase}: {len(deployment.resources[phase])} manifest resources"
            + (f"; Helm: {', '.join(releases)}" if releases else "")
        )
    typer.echo("This component plan does not include models or optional apps.")


def checked(
    profile: Path, root: Path | None, targets: list[str] | None = None
) -> tuple[Deployment, Installation]:
    typer.echo("Preparing profile and composing local manifests...")
    deployment, installation = prepare(profile, root, charts=False, targets=targets)
    p = deployment.profile
    resource_count = sum(len(resources) for resources in deployment.resources.values())
    typer.echo(
        f"Target: {p.context} ({p.platform}, {p.domain}); local composition passed "
        f"({resource_count} resources, {len(deployment.releases)} Helm releases)."
    )
    warnings = check_prerequisites(deployment, progress=lambda message: typer.echo("  " + message))
    for warning in warnings:
        typer.echo("Warning: " + warning)
    typer.echo("Checking resource conflicts...")
    installation.conflicts()
    typer.echo("Resource conflict check passed.")
    return deployment, installation


@app.command()
@guarded
def preflight(
    targets: list[str] | None = typer.Argument(None, help="Components to check; default: core."),
    profile: Path = typer.Option(DEFAULT, "--profile", "-p"),
    root: Path | None = typer.Option(None, "--stack-root"),
) -> None:
    """Read-only prerequisite checks, including rendering and resource conflicts."""
    deployment, _ = checked(profile, root, targets)
    preview(deployment)
    typer.echo("Preflight passed; warnings are not guarantees of external readiness.")


@component_app.command()
@guarded
def install(
    targets: list[str] | None = typer.Argument(None, help="Components to install; default: core."),
    profile: Path = typer.Option(DEFAULT, "--profile", "-p"),
    root: Path | None = typer.Option(None, "--stack-root"),
    dry_run: bool = typer.Option(False, help="Validate, preflight and preview without applying."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm installation noninteractively."),
) -> None:
    """Validate, preflight, then apply the core platform directly through Helm/Kustomize."""
    if targets is None:
        desired = local_inventory(profile, root).state.get("desired", {})
        if "components" in desired:
            targets = desired["components"]
            if not targets:
                raise StackError(
                    "No components selected in inventory.yaml; choose targets explicitly "
                    "or revise the init selection."
                )
    deployment, installation = checked(profile, root, targets)
    preview(deployment)
    if dry_run:
        typer.echo("Dry run complete; no cluster changes made.")
        return
    if not yes and not typer.confirm("Install this platform on the displayed cluster?"):
        raise typer.Abort()
    apply_components(deployment, installation)


def apply_components(deployment: Deployment, installation: Installation) -> None:
    try:
        installation.install()
    except StackError as exc:
        for name in deployment.acting:
            state = installation.state["components"].get(name, {})
            if state.get("status") == "installing":
                state["status"] = "failed"
        installation.save()
        raise StackError(
            f"Install phase {installation.state.get('phase', 'bootstrap')}: {exc}. "
            f"Partial inventory retained at {installation.path}; rerun after correcting the cause"
        ) from None
    typer.echo(f"Platform installed. Inventory: {installation.path}")
    if "certificates" in deployment.acting:
        typer.echo(
            "Public certificate: " + str(installation.path.parent / "platform-certificate.crt")
        )


@app.command("install")
@guarded
def install_profile(
    profile: Path = typer.Option(DEFAULT, "--profile", "-p"),
    root: Path | None = typer.Option(None, "--stack-root"),
    dry_run: bool = typer.Option(False, help="Preview the profile without applying."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm installation noninteractively."),
) -> None:
    """Install the profile's selected components, then its selected addons."""
    inventory = local_inventory(profile, root)
    desired = inventory.state["desired"]
    targets = desired.get("components", defaults(inventory.config.profile.gpu))
    names = desired.get("addons", [])
    if not targets and not names:
        raise StackError("Nothing selected in inventory.yaml; revise the init selection.")
    available = set(resolve(targets)) | {
        name
        for name, state in inventory.state["components"].items()
        if state.get("status") == "installed"
    }
    for name in addon_order(names):
        if name not in ADDONS:
            raise StackError(f"Unknown addon: {name}; choose from {', '.join(ADDONS)}")
        missing = set(ADDONS[name].dependencies) - available
        if missing:
            raise StackError(
                f"Addon {name} requires components: {', '.join(sorted(missing))}; "
                "select them with init or install them individually first."
            )
    typer.echo(f"Profile: {profile}")
    typer.echo("Selected components: " + (", ".join(targets) or "none"))
    typer.echo("Selected addons: " + (", ".join(names) or "none"))
    prepared = checked(profile, root, targets) if targets else None
    if prepared:
        preview(prepared[0])
    if (
        not dry_run
        and not yes
        and not typer.confirm("Install the selected components and addons on this cluster?")
    ):
        raise typer.Abort()
    if prepared and not dry_run:
        apply_components(*prepared)
    for name in names:
        pending = [
            dependency
            for addon_name in addon_order([name])
            for dependency in ADDONS[addon_name].dependencies
            if inventory.state["components"].get(dependency, {}).get("status") != "installed"
        ]
        if dry_run and pending:
            for addon_name in addon_order([name]):
                addon = ADDONS[addon_name](profile=profile, root=stack_root(root, []))
                addon.deployment.render_charts()
            typer.echo(
                f"Addon {name}: local manifests and charts rendered. Live preflight deferred "
                f"until components are installed: {', '.join(pending)}. "
                "Addon readiness has NOT been verified."
            )
        else:
            install_addon(
                name=name,
                profile=profile,
                root=root,
                namespace="",
                chart_version="2.0.17",
                rag_image="",
                uploads_archive=None,
                dry_run=dry_run,
                yes=True,
            )
    if dry_run:
        typer.echo("Profile dry run complete; no cluster changes made.")
    else:
        typer.echo("Profile installation complete.")


@app.command("uninstall")
@guarded
def uninstall_profile(
    profile: Path = typer.Option(DEFAULT, "--profile", "-p"),
    root: Path | None = typer.Option(None, "--stack-root"),
    purge: bool = typer.Option(
        False, help="Also remove managed data/secrets and unused owned CRDs."
    ),
    dry_run: bool = typer.Option(False, help="Preview all recorded items without deletion."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm profile removal."),
    confirm_data_loss: bool = typer.Option(
        False, help="Additional noninteractive purge confirmation."
    ),
) -> None:
    """Remove all recorded addons, then components, regardless of current selections."""
    inventory = local_inventory(profile, root)
    if not inventory.exists:
        raise StackError("No installation inventory; refusing untracked deletion")
    excluded = {"purged"} if purge else {"uninstalled", "purged"}
    names = [
        name
        for name, state in reversed(list(inventory.state["addons"].items()))
        if state.get("status") not in excluded
    ]
    for name in names:
        if name not in ADDONS:
            raise StackError(f"Unknown recorded addon: {name}; refusing partial profile removal")
    names = [name for name in reversed(addon_order(names)) if name in names]
    known = set(inventory.state["components"])
    known.update(
        record["component"]
        for record in inventory.state["resources"]
        if record["component"] in REGISTRY
    )
    targets = [
        name
        for name in sorted(known)
        if inventory.state["components"].get(name, {}).get("status") not in excluded
    ]
    if not names and not targets:
        typer.echo("No recorded components or addons require removal.")
        return
    typer.echo(f"Profile: {profile}; removing recorded addons before components.")
    for name in names:
        uninstall_addon(
            name=name,
            profile=profile,
            root=root,
            purge=purge,
            dry_run=True,
            yes=True,
            confirm_data_loss=True,
        )
    if targets:
        uninstall(
            targets=targets,
            profile=profile,
            root=root,
            purge=purge,
            dry_run=True,
            yes=True,
            confirm_data_loss=True,
        )
    if dry_run:
        typer.echo("Profile preview only; no changes made.")
        return
    if not yes and not typer.confirm("Uninstall all recorded addons and components?"):
        raise typer.Abort()
    if purge:
        typer.echo(
            "Purge destroys managed credentials and PVCs; backing disk deletion follows "
            "the StorageClass reclaim policy. Local secrets.yaml is retained."
        )
        if not confirm_data_loss and not typer.confirm("Confirm permanent managed data loss?"):
            raise typer.Abort()
    for name in names:
        uninstall_addon(
            name=name,
            profile=profile,
            root=root,
            purge=purge,
            dry_run=False,
            yes=True,
            confirm_data_loss=True,
        )
    if targets:
        uninstall(
            targets=targets,
            profile=profile,
            root=root,
            purge=purge,
            dry_run=False,
            yes=True,
            confirm_data_loss=True,
        )
    typer.echo("Profile uninstall complete.")


@component_app.command()
@guarded
def uninstall(
    targets: list[str] | None = typer.Argument(
        None, help="Components to remove; default: recorded."
    ),
    profile: Path = typer.Option(DEFAULT, "--profile", "-p"),
    root: Path | None = typer.Option(None, "--stack-root"),
    purge: bool = typer.Option(
        False, help="Also remove managed data/secrets and unused owned CRDs."
    ),
    dry_run: bool = typer.Option(False, help="Preview recorded resources without deletion."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm platform removal."),
    confirm_data_loss: bool = typer.Option(
        False, help="Additional noninteractive purge confirmation."
    ),
) -> None:
    """Remove only tracked owned resources; preserve data and TLS credentials by default."""
    if targets:
        resolve(targets)
    config = Configuration(profile, load_connector=False, targets=targets)
    deployment = Deployment(config, stack_root(root, []), Runner(config.profile.context), targets)
    installation = Installation(deployment)
    if not installation.exists:
        raise StackError("No installation inventory; refusing untracked deletion")
    installation.bind()
    selected = installation.removal_targets(targets)
    typer.echo(f"Target: {config.profile.context}; installation {installation.state['id']}")
    typer.echo("Removing: " + ", ".join(selected) + "; dependencies not selected will remain.")
    typer.echo("Warning: external workloads using these controllers are not tracked as dependents.")
    if "monitoring" in selected and (
        installation.state["components"].get("ai-gateway", {}).get("status") == "installed"
        and "ai-gateway" not in selected
    ):
        typer.echo("Warning: Tempo will be absent; AI Gateway traces cannot be delivered.")
    for record in installation.state["resources"]:
        if record["component"] not in selected:
            continue
        keep = record["kind"] in {
            "Namespace",
            "CustomResourceDefinition",
            "PersistentVolumeClaim",
            "Secret",
        }
        action = "retain" if keep and not purge else "remove"
        if record["kind"] == "Namespace":
            action = "retain"
        if record["kind"] == "CustomResourceDefinition" and purge:
            action = "remove only if proven unused"
        typer.echo(f"  {action}: {record['kind']} {record.get('namespace', '')}/{record['name']}")
    if dry_run:
        typer.echo("Preview only; no changes made.")
        return
    if not yes and not typer.confirm("Uninstall this platform?"):
        raise typer.Abort()
    if purge:
        typer.echo(
            "Purge destroys managed credentials and PVCs; backing disk deletion follows "
            "the StorageClass reclaim policy. Local secrets.yaml is retained."
        )
        if not confirm_data_loss and not typer.confirm("Confirm permanent managed data loss?"):
            raise typer.Abort()
    for warning in installation.uninstall(purge, selected):
        typer.echo("Warning: " + warning)
    typer.echo(
        "Uninstall complete. Shared namespaces and unselected components remain; "
        "removed Certificates no longer renew."
    )


@component_app.command("list")
@guarded
def list_installed_components(
    profile: Path = typer.Option(DEFAULT, "--profile", "-p"),
    root: Path | None = typer.Option(None, "--stack-root"),
) -> None:
    """List recorded components and their local inventory status."""
    installation = local_inventory(profile, root)
    components = installation.state["components"]
    typer.echo("Recorded components (local inventory; live health is not checked):")
    for name in sorted(components):
        typer.echo(f"  {name}: {components[name]['status']}")
    if not components:
        typer.echo("  none")


@addon_app.command("list")
@guarded
def list_installed_addons(
    profile: Path = typer.Option(DEFAULT, "--profile", "-p"),
    root: Path | None = typer.Option(None, "--stack-root"),
) -> None:
    """List recorded addons and their local inventory status."""
    installation = local_inventory(profile, root)
    addons = installation.state["addons"]
    typer.echo("Recorded addons (local inventory; live health is not checked):")
    for name in sorted(addons):
        typer.echo(f"  {name}: {addons[name]['status']}")
    if not addons:
        typer.echo("  none")


@addon_app.command("install")
@guarded
def install_addon(
    name: str | None = typer.Argument(
        None, help=f"Addon to install; omit to use inventory.yaml selection ({', '.join(ADDONS)})."
    ),
    profile: Path = typer.Option(DEFAULT, "--profile", "-p"),
    root: Path | None = typer.Option(None, "--stack-root"),
    namespace: str = typer.Option("", help="LibreChat namespace (default: librechat)."),
    chart_version: str = typer.Option("2.0.17", help="LibreChat Helm chart version."),
    rag_image: str = typer.Option("", help="Override the pinned RAG API image."),
    uploads_archive: Path | None = typer.Option(
        None, help="Restore an existing uploads backup tar before upgrading."
    ),
    dry_run: bool = typer.Option(False, help="Run preflight and render without applying."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm installation noninteractively."),
) -> None:
    """Validate, preflight, then install or reconcile an addon."""
    if name:
        names = [name]
    else:
        names = local_inventory(profile, root).state.get("desired", {}).get("addons", [])
        if not names:
            raise StackError(
                "No addons selected in inventory.yaml; name an addon or revise the init selection."
            )
    selected = set(names)
    ordered = addon_order(names)
    if "librechat" not in selected and (
        namespace or chart_version != "2.0.17" or rag_image or uploads_archive
    ):
        raise StackError(
            "--namespace, --chart-version, --rag-image and --uploads-archive "
            "apply only to LibreChat"
        )
    stack = stack_root(root, [])
    typer.echo("Addon installation order: " + ", ".join(ordered))
    confirmed = yes
    pending: set[str] = set()
    for selected_name in ordered:
        addon_type = ADDONS[selected_name]
        addon = addon_type(
            profile=profile,
            root=stack,
            **(
                {
                    "namespace": namespace or "librechat",
                    "chart_version": chart_version,
                    **({"rag_image": rag_image} if rag_image else {}),
                    "uploads_archive": uploads_archive,
                }
                if selected_name == "librechat"
                else {}
            ),
        )
        if (
            selected_name not in selected
            and addon.installation.state["addons"].get(selected_name, {}).get("status")
            == "installed"
        ):
            typer.echo(f"Dependency {selected_name} already installed; checking via its dependent.")
            continue
        deferred = pending.intersection(getattr(addon_type, "addon_dependencies", ()))
        if dry_run and deferred:
            addon.deployment.render_charts()
            typer.echo(
                f"Addon {selected_name}: local manifests and charts rendered. "
                "Live preflight deferred until addons are installed: "
                f"{', '.join(sorted(deferred))}. "
                "Addon readiness has NOT been verified."
            )
            pending.add(selected_name)
            continue
        values, db_password, warnings = addon.check(
            progress=lambda message: typer.echo("  " + message)
        )
        typer.echo(
            f"Target: {addon.config.profile.context}; addon {selected_name}; "
            f"namespace {getattr(addon, 'namespace', selected_name)}"
        )
        typer.echo(
            f"Rendered {sum(len(docs) for docs in addon.deployment.templates.values())} "
            f"chart resources and "
            f"{sum(len(docs) for docs in addon.deployment.resources.values())} addon manifests."
        )
        if values.get("ENKI_GATEWAY_API_KEY") == "sk-local-vllm":
            typer.echo(
                "Warning: using placeholder gateway API key; "
                "set a real key if authentication is enabled."
            )
        if selected_name == "librechat" and not values.get("FIRECRAWL_API_KEY"):
            typer.echo("Warning: Crawl4AI Firecrawl shim key is missing; web scraping may fail.")
        for warning in warnings:
            typer.echo("Warning: " + warning)
        if dry_run:
            pending.add(selected_name)
            continue
        if not confirmed:
            if not typer.confirm(
                "Install the selected addon(s) and their dependencies on this cluster?"
            ):
                raise typer.Abort()
            confirmed = True
        addon.install(values, db_password)
        typer.echo(f"Addon {selected_name} installed. Inventory: {addon.installation.path}")
    if dry_run:
        typer.echo("Dry run complete; no cluster changes made.")
        return


@addon_app.command("uninstall")
@guarded
def uninstall_addon(
    name: str = typer.Argument(..., help=f"Addon to remove: {', '.join(ADDONS)}."),
    profile: Path = typer.Option(DEFAULT, "--profile", "-p"),
    root: Path | None = typer.Option(None, "--stack-root"),
    purge: bool = typer.Option(False, help="Also delete managed addon data and secrets."),
    dry_run: bool = typer.Option(False, help="Preview recorded resources without deletion."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm addon removal."),
    confirm_data_loss: bool = typer.Option(
        False, help="Additional noninteractive purge confirmation."
    ),
) -> None:
    """Remove an addon from the shared installation inventory."""
    if name not in ADDONS:
        raise StackError(f"Unknown addon: {name}; choose from {', '.join(ADDONS)}")
    config = Configuration(profile, load_connector=False, targets=[])
    deployment = Deployment(
        config, stack_root(root, []), Runner(config.profile.context), targets=[]
    )
    installation = Installation(deployment)
    records = installation.addon_removal_preview(name, purge)
    typer.echo(f"Target: {config.profile.context}; addon {name}")
    for action, kind, namespace, resource_name in records:
        typer.echo(f"  {action}: {kind} {namespace}/{resource_name}")
    if dry_run:
        typer.echo("Preview only; no changes made.")
        return
    if not yes and not typer.confirm(f"Uninstall addon {name}?"):
        raise typer.Abort()
    if purge:
        typer.echo(
            "Purge deletes managed credentials and PVCs; backing disk behavior follows "
            "the StorageClass reclaim policy. Local secrets.yaml is retained."
        )
        if not confirm_data_loss and not typer.confirm("Confirm permanent addon data loss?"):
            raise typer.Abort()
    for warning in installation.uninstall_addon(name, purge):
        typer.echo("Warning: " + warning)
    if purge:
        typer.echo(f"Addon {name} purged; local secrets.yaml and namespace remain.")
    else:
        typer.echo(f"Addon {name} uninstalled; retained credentials and data remain available.")
