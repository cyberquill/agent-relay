"""Queue modes: what a phone message does while a session is mid-turn, and sending waiting messages
as the next turn when the turn ends."""

from __future__ import annotations

from agent_relay.adapters.base import UnsupportedError
from agent_relay.core import updates
from agent_relay.core.service.common import Deps, RelayError
from agent_relay.log import get_logger
from agent_relay.models.config import QueueMode
from agent_relay.models.events import ErrorEvent, Event, Harness, TurnCompleted
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

QUEUE_MODE_KEY = "queue_mode"
# Same as core.hooks.HOOKED, which cannot be imported here (hooks -> approvals -> service); a test pins them.
HOOKED = "hooked"
MODES: tuple[QueueMode, ...] = ("queue", "steer", "interrupt")
# These adapters pop the queue themselves in their turn-end path.
SELF_DRAINING = frozenset({Harness.CURSOR_CLI, Harness.CURSOR_CDP})


def mode_of(session: SessionRecord, default: QueueMode) -> QueueMode:
    stored = session.extra.get(QUEUE_MODE_KEY)
    return next((m for m in MODES if m == stored), default)


def parse_mode(word: str) -> QueueMode:
    lowered = word.strip().lower()
    for mode in MODES:
        if mode == lowered:
            return mode
    raise RelayError(f"Unknown queue mode `{word}`. Use queue, steer or interrupt.")


class QueueDrain:
    """Router sink: when a turn ends, send what is waiting as one new turn.

    Only for sessions an adapter drives. Self-draining adapters and hook-driven sessions deliver
    through their own turn-end path (the stop hook, the idle notification).
    """

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    async def handle(self, event: Event) -> None:
        if isinstance(event, TurnCompleted):
            await self._drain(event.session_key)

    async def _drain(self, session_key: str) -> None:
        deps = self._deps
        session = await deps.registry.get_session(session_key)
        if (
            session is None
            or session.harness in SELF_DRAINING
            or session.extra.get(HOOKED) is True
            or session.status in (SessionStatus.ENDED, SessionStatus.PARKED)
        ):
            return
        adapter = deps.router.adapter(session.harness)
        if adapter is None or await updates.degraded_reason(deps.registry, session.harness) is not None:
            return
        queued = await deps.registry.pop_queued(session_key, ("prompt", "steer"))
        if not queued:
            return
        text = "\n\n".join(q.text for q in queued)
        try:
            try:
                await adapter.send_prompt(session, text)
            except UnsupportedError:
                await adapter.wake(session, text)
        except Exception as exc:
            log.warning(f"{session_key}: could not send the queued message ({type(exc).__name__}: {exc})")
            await deps.registry.enqueue(session_key, "prompt", text)
            await deps.router.publish(
                ErrorEvent(
                    harness=session.harness,
                    session_key=session_key,
                    workspace=session.workspace,
                    message=f"Could not send your queued message; it is still queued. ({exc})",
                )
            )
            return
        await deps.lifecycle.observe(session_key, SessionStatus.RUNNING, "queued message sent")
