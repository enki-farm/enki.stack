from collections.abc import Iterable

from enkictl.components import (
    ai_gateway,
    auth,
    certificates,
    gpu_operator,
    kserve,
    kubeflow_hub,
    monitoring,
)
from enkictl.config import StackError

REGISTRY = {
    item.name: item
    for item in (
        certificates.COMPONENT,
        ai_gateway.COMPONENT,
        auth.COMPONENT,
        monitoring.COMPONENT,
        gpu_operator.COMPONENT,
        kserve.COMPONENT,
        kubeflow_hub.COMPONENT,
    )
}


def defaults(gpu: str | None) -> list[str]:
    return [name for name in REGISTRY if name != "gpu-operator" or gpu == "operator"]


def phases(components: Iterable[str]) -> list[str]:
    return [phase for name in components for phase in REGISTRY[name].phases]


def resolve(targets: Iterable[str]) -> list[str]:
    result: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name not in REGISTRY:
            raise StackError(f"Unknown component: {name}; choose from {', '.join(REGISTRY)}")
        if name in visiting:
            raise StackError(f"Component dependency cycle at {name}")
        if name in result:
            return
        visiting.add(name)
        for dependency in REGISTRY[name].dependencies:
            visit(dependency)
        visiting.remove(name)
        result.append(name)

    for target in targets:
        visit(target)
    return result


def removal(targets: Iterable[str], active: Iterable[str]) -> list[str]:
    selected = set(targets)
    resolve(selected)
    for name in active:
        if name not in selected and selected.intersection(resolve([name])):
            raise StackError(f"Cannot remove dependency of {name}; remove {name} explicitly first")
    return [name for name in reversed(resolve(REGISTRY)) if name in selected]
