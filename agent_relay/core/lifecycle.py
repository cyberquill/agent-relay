"""The one owner of session status. Every move goes through here, together with its cleanup.

Ending a session or stopping it must not leave a hook parked, a phone card answerable, a queued
message waiting for a turn that will never come, or a grant that outlives the session.
"""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.adapters.base import EventPublisher
from agent_relay.core.keys import RELEASE_KEY, pending_key, session_signal
from agent_relay.core.registry import Registry
from agent_relay.core.signals import Signals
from agent_relay.log import get_logger
from agent_relay.models.events import ApprovalResolved, QuestionAnswered, ResolvedBy, SessionReleased
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus

log = get_logger(__name__)

# Hook activity proves a session is alive, so an ended one may come back as running; a phone
# reply revives it as idle. Nothing else leaves `ended`, and nothing goes back to `active`.
ALLOWED: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.ACTIVE: frozenset(
        {SessionStatus.RUNNING, SessionStatus.IDLE, SessionStatus.PARKED, SessionStatus.ENDED}
    ),
    SessionStatus.RUNNING: frozenset({SessionStatus.IDLE, SessionStatus.PARKED, SessionStatus.ENDED}),
    SessionStatus.IDLE: frozenset({SessionStatus.RUNNING, SessionStatus.PARKED, SessionStatus.ENDED}),
    SessionStatus.PARKED: frozenset({SessionStatus.RUNNING, SessionStatus.IDLE, SessionStatus.ENDED}),
    SessionStatus.ENDED: frozenset({SessionStatus.IDLE, SessionStatus.RUNNING}),
}
LIVE = frozenset({SessionStatus.ACTIVE, SessionStatus.RUNNING, SessionStatus.PARKED})


class UnknownSessionError(LookupError):
    pass


class InvalidTransitionError(RuntimeError):
    pass


class Lifecycle:
    def __init__(self, registry: Registry, publisher: EventPublisher, signals: Signals) -> None:
        self._registry = registry
        self._publisher = publisher
        self._signals = signals

    async def transition(self, session_key: str, to: SessionStatus, reason: str) -> SessionRecord:
        """Move a session to `to`, validating the move. Ending it also cleans up after it."""
        session = await self._session(session_key)
        if session.status is to:
            return session
        if to not in ALLOWED[session.status]:
            raise InvalidTransitionError(
                f"{session_key}: {session.status.value} -> {to.value} is not allowed"
            )
        await self._registry.set_session_status(session_key, to)
        if to is SessionStatus.ENDED:
            await self._wind_down(session, "ended", reason)
            await self._registry.drop_session_grants(session_key)
        log.debug(f"{session_key}: {session.status.value} -> {to.value} ({reason})")
        return await self._session(session_key)

    async def observe(self, session_key: str, to: SessionStatus, reason: str) -> SessionRecord | None:
        """Record a status an adapter or hook reports. A report the state machine refuses is dropped."""
        try:
            return await self.transition(session_key, to, reason)
        except (UnknownSessionError, InvalidTransitionError) as exc:
            log.debug(f"ignored reported status: {exc}")
            return None

    async def revive(self, session_key: str, reason: str) -> SessionRecord:
        """Bring an ended session back to idle (the user replied in its thread)."""
        session = await self._session(session_key)
        if session.status is not SessionStatus.ENDED:
            return session
        return await self.transition(session_key, SessionStatus.IDLE, reason)

    async def stop(self, session_key: str, reason: str = "stopped", *, settle: bool = True) -> SessionRecord:
        """A stop from the phone: release the parked hook, close its requests, drop queued messages.

        `settle` also marks a live session idle; pass False when the agent could not be cancelled.
        """
        session = await self._session(session_key)
        await self._wind_down(session, "stopped", reason)
        if settle and session.status in LIVE:
            return await self.transition(session_key, SessionStatus.IDLE, reason)
        return session

    async def release(self, session_key: str, reason: str) -> None:
        """Wake a parked hook so it returns control to the agent."""
        await self._registry.kv_set(RELEASE_KEY.format(session_key=session_key), reason)
        self._signals.notify(session_signal(session_key))
        session = await self._registry.get_session(session_key)
        if session is not None:
            await self._publisher.publish(
                SessionReleased(
                    harness=session.harness,
                    session_key=session_key,
                    workspace=session.workspace,
                    reason=reason,
                )
            )

    async def _session(self, session_key: str) -> SessionRecord:
        found = await self._registry.get_session(session_key)
        if found is None:
            raise UnknownSessionError(session_key)
        return found

    async def _wind_down(self, session: SessionRecord, by: ResolvedBy, reason: str) -> None:
        if session.status is SessionStatus.PARKED:
            await self.release(session.session_key, reason)
        for request in await self._registry.open_pending():
            if request.session_key == session.session_key:
                await self._close_request(session, request, by)
        await self._registry.drop_queued(session.session_key)

    async def _close_request(self, session: SessionRecord, request: PendingRequest, by: ResolvedBy) -> None:
        answer: dict[str, JsonValue]
        if request.kind == "approval":
            answer = {"decision": "deny", "by": by}
        else:
            answer = {"answer": None, "by": by}
        if not await self._registry.resolve_pending(request.request_id, answer, status="expired"):
            return
        self._signals.notify(pending_key(request.request_id))
        if request.kind == "approval":
            await self._publisher.publish(
                ApprovalResolved(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    request_id=request.request_id,
                    decision="deny",
                    by=by,
                )
            )
        else:
            await self._publisher.publish(
                QuestionAnswered(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    request_id=request.request_id,
                    answer=None,
                    by=by,
                )
            )
