from __future__ import annotations

from typing import TYPE_CHECKING

from enkictl.components.base import Component

if TYPE_CHECKING:
    from enkictl.render import Deployment


def compose(d: Deployment) -> None:
    pass


COMPONENT = Component(
    "kserve",
    ("certificates", "ai-gateway"),
    ("kserve",),
    ("kserve",),
    compose,
    ("helm/kserve",),
)
