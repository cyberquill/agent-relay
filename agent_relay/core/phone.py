"""Which session a `relay-mcp` call belongs to, and whether it may reach the phone at all.

An MCP server only knows its working directory, so the call is matched to the live session whose
workspace contains it (busy sessions first, then the most recently updated).
"""

from __future__ import annotations

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.registry import Registry
from agent_relay.models.events import Origin
from agent_relay.models.sessions import SessionRecord, SessionStatus

BUSY = (SessionStatus.RUNNING, SessionStatus.PARKED)
NO_SESSION = "no relay session is live for this workspace"
AWAY_OFF = "away mode is off for this workspace"
PAUSED = "the relay is paused (/panic); send /resume from the phone"


def _contains(root: str, workspace: str) -> int | None:
    """How deep `workspace` sits under `root` (0 = same folder), or None when it is outside."""
    try:
        relative = paths.expand_user(workspace).resolve().relative_to(paths.expand_user(root).resolve())
    except ValueError:
        return None
    return len(relative.parts)


async def session_for(registry: Registry, workspace: str) -> SessionRecord | None:
    candidates: list[tuple[int, bool, float, SessionRecord]] = []
    for session in await registry.list_sessions():
        if session.status is SessionStatus.ENDED or not session.workspace:
            continue
        depth = _contains(session.workspace, workspace)
        if depth is not None:
            candidates.append((depth, session.status in BUSY, session.updated.timestamp(), session))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[1], c[0], -c[2]))
    return candidates[0][3]


async def reachable(registry: Registry, workspace: str) -> tuple[SessionRecord | None, str]:
    """The session to post into, or None with the reason the phone is not reachable."""
    if paths.pause_flag().exists():
        return None, PAUSED
    session = await session_for(registry, workspace)
    if session is None:
        return None, NO_SESSION
    if session.origin is not Origin.PHONE and not await away.is_away(registry, session.workspace):
        return None, AWAY_OFF
    return session, ""
