from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from enkictl.lifecycle import Installation
    from enkictl.render import Deployment


@dataclass(frozen=True)
class Component:
    name: str
    dependencies: tuple[str, ...]
    phases: tuple[str, ...]
    namespaces: tuple[str, ...]
    compose: Callable[[Deployment], None]
    assets: tuple[str, ...] = ()
    storage: bool = False
    generated_credentials: tuple[str, ...] = ()
    required_credentials: tuple[str, ...] = ()
    secret_names: tuple[tuple[str, str], ...] = ()
    credentials: Callable[[Deployment], list[Secret]] | None = None
    ready: Callable[[Installation, str], None] | None = None


@dataclass(frozen=True)
class Secret:
    namespace: str
    name: str
    values: dict[str, str]
    kind: str = "Opaque"


COMPONENT_LABEL = "enki.stack/component"
