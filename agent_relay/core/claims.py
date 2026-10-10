"""Workspace claims: link a phone-started VS Code chat to the hook session it produces.

VS Code does not hand back an id when a chat is opened through a command, and the hooks of an Agent
Host session report an id of their own. The adapter records a claim for the workspace instead; the
first unmatched VS Code hook from that workspace binds to it. A claim on a session that already has
a harness id becomes an alias from the hook's session id to the session.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from agent_relay import paths
from agent_relay.core.registry import Registry

CLAIM_KEY = "vscode_claim:{workspace}"
ALIAS_KEY = "hook_alias:{hook_session_id}"
CLAIM_TTL_S = 120.0
UNCLAIMED_PREFIX = "unclaimed-"


@dataclass(frozen=True)
class Claim:
    session_key: str
    expires: float


def claim_key(workspace: str) -> str:
    return CLAIM_KEY.format(workspace=paths.expand_user(workspace).resolve())


async def make_claim(
    registry: Registry,
    workspace: str,
    session_key: str,
    ttl: float = CLAIM_TTL_S,
    clock: Callable[[], float] = time.time,
) -> None:
    value = json.dumps({"session_key": session_key, "expires": clock() + ttl})
    await registry.kv_set(claim_key(workspace), value)


async def take_claim(
    registry: Registry, workspace: str | None, clock: Callable[[], float] = time.time
) -> Claim | None:
    """Pop the workspace's claim if it is still live. Expired or malformed claims are dropped."""
    if not workspace:
        return None
    raw = await registry.kv_pop(claim_key(workspace))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        claim = Claim(session_key=str(data["session_key"]), expires=float(data["expires"]))
    except (ValueError, KeyError, TypeError):
        return None
    return claim if claim.expires > clock() else None


async def link_alias(registry: Registry, hook_session_id: str, session_key: str) -> None:
    await registry.kv_set(ALIAS_KEY.format(hook_session_id=hook_session_id), session_key)


async def aliased(registry: Registry, hook_session_id: str) -> str | None:
    return await registry.kv_get(ALIAS_KEY.format(hook_session_id=hook_session_id))
