"""Questions the agent asks the user, and the relay's one answer to every tool permission: allow.

There are no approval cards. A tool permission the relay is asked about is allowed at once: hooks answer
it in `core/hooks.py`, adapter-held requests are answered here by `intercept` before any sink sees them.
Questions (`ask_phone`, an app's own input request) keep a pending row, a card and a deadline.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import DeliveryError, RelayService, pending_key
from agent_relay.core.signals import Signals
from agent_relay.log import get_logger
from agent_relay.models.config import QuestionsConfig
from agent_relay.models.events import (
    ApprovalRequested,
    Event,
    QuestionAnswered,
    QuestionAsked,
)
from agent_relay.models.sessions import PendingRequest, SessionRecord

log = get_logger(__name__)

SWEEP_GIVE_UP = timedelta(minutes=10)


class ApprovalBroker:
    def __init__(
        self,
        registry: Registry,
        router: Router,
        signals: Signals,
        service: RelayService,
        config: QuestionsConfig,
    ) -> None:
        self._registry = registry
        self._router = router
        self._signals = signals
        self._service = service
        self._config = config
        router.add_gate(self)

    # questions

    async def ask(
        self, session: SessionRecord, question: str, options: tuple[str, ...], deadline_s: float | None = None
    ) -> str | None:
        """Post a question and wait for the answer (None when skipped or expired)."""
        answer, _ = await self.ask_outcome(session, question, options, deadline_s)
        return answer

    async def ask_outcome(
        self,
        session: SessionRecord,
        question: str,
        options: tuple[str, ...],
        deadline_s: float | None = None,
        source: str = "hook",
    ) -> tuple[str | None, str]:
        """Like `ask`, plus who settled it: `phone`, `deadline`, `panic` or `desk`."""
        request_id = f"ask-{uuid.uuid4().hex[:12]}"
        timeout = float(deadline_s if deadline_s is not None else self._config.deadline_s)
        deadline = datetime.now(UTC) + timedelta(seconds=timeout)
        await self._registry.add_pending(
            PendingRequest(
                request_id=request_id,
                kind="question",
                session_key=session.session_key,
                payload={"source": source, "question": question},
                deadline=deadline,
            )
        )
        await self._router.publish(
            QuestionAsked(
                harness=session.harness,
                session_key=session.session_key,
                workspace=session.workspace,
                request_id=request_id,
                question=question,
                options=options,
            )
        )
        resolved = await self._wait_or_abandon(request_id, deadline)
        if resolved is None:
            if await self._service.answer_question(request_id, None, by="deadline"):
                return None, "deadline"
            request = await self._registry.get_pending(request_id)
            resolved = request.answer if request and request.answer else {}
        answer = resolved.get("answer")
        return (answer if isinstance(answer, str) else None), str(resolved.get("by", "phone"))

    async def _wait_or_abandon(self, request_id: str, deadline: datetime) -> dict[str, JsonValue] | None:
        """`_wait`, but if the hook goes away, close the request so its card stops being answerable."""
        try:
            return await self._wait(request_id, deadline)
        except asyncio.CancelledError:
            await asyncio.shield(self.abandon(request_id))
            raise

    async def abandon(self, request_id: str) -> bool:
        """Close an open question whose agent is gone. False when it was already answered."""
        request = await self._registry.get_pending(request_id)
        if request is None:
            return False
        answer: dict[str, JsonValue] = {"answer": None, "by": "abandoned"}
        if not await self._registry.resolve_pending(request_id, answer, status="expired"):
            return False
        log.info(f"{request_id}: the agent went away; closed")
        session = await self._registry.get_session(request.session_key)
        if session is None or request.kind != "question":
            return True
        await self._router.publish(
            QuestionAnswered(
                harness=session.harness,
                session_key=session.session_key,
                workspace=session.workspace,
                request_id=request_id,
                answer=None,
                by="abandoned",
            )
        )
        return True

    async def _wait(self, request_id: str, deadline: datetime) -> dict[str, JsonValue] | None:
        while True:
            request = await self._registry.get_pending(request_id)
            if request is not None and request.status != "open":
                return request.answer or {}
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return None
            await self._signals.wait(pending_key(request_id), min(remaining, 30.0))

    # tool permissions: always allow

    async def _relay_answers_for(self, session: SessionRecord) -> bool:
        """The relay answers for phone-held sessions and, while away, for desk sessions. A desk session
        at the desk keeps the app's own prompt; after a panic nobody is answered."""
        if paths.pause_flag().exists():
            return False
        return away.phone_held(session) or await away.is_away(self._registry, session.workspace)

    async def intercept(self, event: Event) -> bool:
        """Router gate: an adapter-held tool permission is allowed here, so it never reaches a sink and
        the phone gets no card or push for it. False leaves the request to the app's own prompt."""
        if not isinstance(event, ApprovalRequested):
            return False
        session = await self._registry.get_session(event.session_key)
        if session is None or not await self._relay_answers_for(session):
            return False
        adapter = self._router.adapter(session.harness)
        if adapter is None:
            log.warning(f"{event.request_id}: {session.harness.value} is not connected; left to the app")
            return False
        try:
            await adapter.answer_approval(session, event.request_id, "allow")
        except Exception as exc:
            log.warning(f"{event.request_id}: could not allow {event.tool} in {session.harness.value}: {exc}")
            return False
        log.info(f"{event.request_id}: {event.tool} allowed (auto)")
        return True

    # adapter-held questions arrive as events

    async def _store_adapter_question(self, event: QuestionAsked) -> None:
        await self._registry.add_pending(
            PendingRequest(
                request_id=event.request_id,
                kind="question",
                session_key=event.session_key,
                payload={"source": "adapter", "native_id": event.request_id, "question": event.question},
                deadline=datetime.now(UTC) + timedelta(seconds=self._config.deadline_s),
            )
        )

    async def handle(self, event: Event) -> None:
        if isinstance(event, QuestionAnswered):
            await self._settled_elsewhere(event)
            return
        if not isinstance(event, QuestionAsked):
            return
        if await self._registry.get_pending(event.request_id) is not None:
            return
        await self._store_adapter_question(event)

    async def _settled_elsewhere(self, event: QuestionAnswered) -> None:
        """An adapter saw the question answered at the desk: close it without answering natively again.
        Events from our own answers find the request already resolved, so this is idempotent."""
        answer: dict[str, JsonValue] = {"answer": event.answer, "by": "desk"}
        if await self._registry.resolve_pending(event.request_id, answer):
            self._signals.notify(pending_key(event.request_id))
            log.info(f"{event.request_id} was answered at the desk")

    async def sweep(self) -> int:
        """Skip every open question past its deadline. Returns how many expired.

        A question whose app cannot be reached stays open and is retried; after `SWEEP_GIVE_UP` past its
        deadline it is closed as expired so a vanished app cannot keep it alive forever. An approval row
        left by an older version is closed without being delivered: nothing is waiting on it.
        """
        expired = 0
        now = datetime.now(UTC)
        for request in await self._registry.open_pending():
            if request.deadline > now:
                continue
            if request.kind != "question":
                done = await self._registry.resolve_pending(
                    request.request_id, {"decision": "allow", "by": "deadline"}, status="expired"
                )
                expired += int(done)
                continue
            try:
                done = await self._service.answer_question(request.request_id, None, by="deadline")
            except DeliveryError as exc:
                done = await self._give_up_or_retry(request, now, exc)
            expired += int(done)
        return expired

    async def _give_up_or_retry(self, request: PendingRequest, now: datetime, exc: DeliveryError) -> bool:
        if now - request.deadline < SWEEP_GIVE_UP:
            log.warning(f"could not expire {request.request_id}, will retry: {exc}")
            return False
        answer: dict[str, JsonValue] = {"answer": None, "by": "deadline"}
        closed = await self._registry.resolve_pending(request.request_id, answer, status="expired")
        if closed:
            self._signals.notify(pending_key(request.request_id))
            log.warning(f"gave up on {request.request_id}: {exc}")
        return closed

    async def sweeper(self, interval: float = 15.0) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("pending sweep failed")
            await asyncio.sleep(interval)
