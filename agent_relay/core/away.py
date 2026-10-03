"""Away-mode resolution: a workspace-specific setting wins over the global one."""

from __future__ import annotations

from pathlib import Path

from agent_relay import paths
from agent_relay.core.registry import GLOBAL_SCOPE, Registry


def scope_for(workspace: str | None) -> str:
    if workspace is None:
        return GLOBAL_SCOPE
    return str(paths.expand_user(workspace).resolve())


def resolve(scopes: dict[str, bool], workspace: str | None) -> bool:
    if workspace is not None:
        target = Path(scope_for(workspace))
        best: tuple[int, bool] | None = None
        for scope, enabled in scopes.items():
            if scope == GLOBAL_SCOPE:
                continue
            root = Path(scope)
            if target == root or root in target.parents:
                depth = len(root.parts)
                if best is None or depth > best[0]:
                    best = (depth, enabled)
        if best is not None:
            return best[1]
    return scopes.get(GLOBAL_SCOPE, False)


async def is_away(registry: Registry, workspace: str | None) -> bool:
    if paths.pause_flag().exists():
        return False
    return resolve(await registry.away_scopes(), workspace)


async def set_away(registry: Registry, enabled: bool, workspace: str | None = None) -> None:
    await registry.set_away(enabled, scope_for(workspace))
