"""Away mode: one global, manual flag (decision U5), plus the rule for desk sessions.

Away mode is switched only by `/away` and `/back` (phone or desk chat) and `relay away on|off`.
Nothing flips it on its own. The `workspace` argument of `is_away` and `set_away` is accepted and
ignored, so call sites written for per-workspace scopes (and old skill copies) keep working.

The desk rule: a session started at the desk (`origin=desk`, or a phone chat handed back to the desk)
is the phone's business only while away mode is on. While it is off, the router withholds such a
session's events from the presentation sinks, so an adopted desk chat stops streaming to the phone
the moment away goes off. Phone-held sessions and `adapter:` pseudo-sessions always pass.
"""

from __future__ import annotations

from agent_relay import paths
from agent_relay.core.registry import GLOBAL_SCOPE, Registry
from agent_relay.models.events import Event, Harness, Origin
from agent_relay.models.sessions import SessionRecord

# Session extra: a phone session the user took back at the desk.
AT_DESK = "at_desk"
# Session extra: every folder of a multi-root window (the VS Code adapter records it).
FOLDERS = "folders"
# Apps whose phone-started chats also show in a desktop window, where the user can answer them.
DESK_VISIBLE = frozenset({Harness.VSCODE, Harness.VSCODE_COPILOT, Harness.CURSOR_CDP})
PSEUDO_PREFIX = "adapter:"


def phone_held(session: SessionRecord) -> bool:
    """Whether the phone answers for this session whatever away mode says (D13, D112)."""
    return session.origin is Origin.PHONE and session.extra.get(AT_DESK) is not True


async def is_away(registry: Registry, workspace: str | None = None) -> bool:
    """Whether away mode is on. A pause (`/panic`) counts as at the desk. `workspace` is ignored."""
    if paths.pause_flag().exists():
        return False
    return (await registry.away_scopes()).get(GLOBAL_SCOPE, False)


async def set_away(registry: Registry, enabled: bool, workspace: str | None = None) -> None:
    """Switch the one global flag. `workspace` is ignored."""
    await registry.set_away(enabled, GLOBAL_SCOPE)


class DeskGate:
    """Tells the router which events the phone may see: desk sessions only while away mode is on."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    async def visible(self, event: Event) -> bool:
        if event.session_key.startswith(PSEUDO_PREFIX):
            return True
        session = await self._registry.get_session(event.session_key)
        if session is None or phone_held(session):
            return True
        return await is_away(self._registry)
