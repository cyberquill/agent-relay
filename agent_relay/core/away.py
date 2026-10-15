"""Away-mode resolution: a workspace-specific setting wins over the global one."""

from __future__ import annotations

from pathlib import Path

from agent_relay import paths
from agent_relay.core.registry import GLOBAL_SCOPE, Registry
from agent_relay.models.events import Harness, Origin
from agent_relay.models.sessions import SessionRecord

# Session extras: a phone session the user took back at the desk, and every folder of a multi-root window.
AT_DESK = "at_desk"
FOLDERS = "folders"
# Apps whose phone-started chats also show in a desktop window, where the user can answer them.
DESK_VISIBLE = frozenset({Harness.VSCODE, Harness.VSCODE_COPILOT, Harness.CURSOR_CDP})


def scope_for(workspace: str | None) -> str:
    if workspace is None:
        return GLOBAL_SCOPE
    return str(paths.expand_user(workspace).resolve())


def phone_held(session: SessionRecord) -> bool:
    """Whether the phone answers for this session whatever away mode says (D13, D112)."""
    return session.origin is Origin.PHONE and session.extra.get(AT_DESK) is not True


def session_roots(session: SessionRecord) -> list[str]:
    folders = session.extra.get(FOLDERS)
    listed = [f for f in folders if isinstance(f, str)] if isinstance(folders, list) else []
    return [root for root in (session.workspace, *listed) if root]


def covers(workspace: str | None, session: SessionRecord) -> bool:
    """Whether an away scope (None for every workspace) holds any folder of the session."""
    if workspace is None:
        return True
    scope = Path(scope_for(workspace))
    for root in session_roots(session):
        target = Path(scope_for(root))
        if target == scope or scope in target.parents:
            return True
    return False


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
