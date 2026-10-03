"""Pending approvals and questions: creation, waiting, the relay's own deadline, and expiry.

Hook-held requests block the hook until answered or until the deadline, which is always below the
hook timeout, and then return an explicit deny. Adapter-held requests are answered natively; the
sweeper denies them at the deadline.
"""

from __future__ import annotations

import asyncio
import fnmatch
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from agent_relay.adapters.base import Decision
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayService, pending_key
from agent_relay.core.signals import Signals
from agent_relay.log import get_logger
from agent_relay.models.config import ApprovalConfig
from agent_relay.models.events import (
    ApprovalRequested,
    ApprovalResolved,
    Event,
    QuestionAnswered,
    QuestionAsked,
)
from agent_relay.models.sessions import PendingRequest, SessionRecord

log = get_logger(__name__)

ALLOW_SESSION_KEY = "allow_session:{session_key}:{tool}"
DEADLINE_REASON = "No answer from the phone before the relay's deadline; denied."


@dataclass(frozen=True)
class Outcome:
    decision: Decision
    by: str

    @property
    def allowed(self) -> bool:
        return self.decision != "deny"


def is_read_only(tool: str | None, patterns: list[str]) -> bool:
    if not tool:
        return False
    name = tool.lower().rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, p.lower()) for p in patterns)


class ApprovalBroker:
    def __init__(
        self,
        registry: Registry,
        router: Router,
        signals: Signals,
        service: RelayService,
        config: ApprovalConfig,
    ) -> None:
        self._registry = registry
        self._router = router
        self._signals = signals
        self._service = service
        self._config = config

    # policy

    def auto_allowed(self, tool: str | None) -> bool:
        return is_read_only(tool, self._config.auto_allow)

    async def allowed_for_session(self, session_key: str, tool: str | None) -> bool:
        if not tool:
            return False
        return (
            await self._registry.kv_get(ALLOW_SESSION_KEY.format(session_key=session_key, tool=tool))
            is not None
        )

    # hook-held approvals

    async def request(
        self, session: SessionRecord, tool: str, detail: str, deadline_s: float | None = None
    ) -> Outcome:
        """Post an approval and wait for the answer or the deadline (explicit deny)."""
        if await self.allowed_for_session(session.session_key, tool):
            return Outcome("allow", "session")
        request_id = f"hook-{uuid.uuid4().hex[:12]}"
        timeout = float(deadline_s if deadline_s is not None else self._config.deadline_s)
        deadline = datetime.now(UTC) + timedelta(seconds=timeout)
        await self._registry.add_pending(
            PendingRequest(
                request_id=request_id,
                kind="approval",
                session_key=session.session_key,
                payload={"source": "hook", "tool": tool, "detail": detail},
                deadline=deadline,
            )
        )
        await self._router.publish(
            ApprovalRequested(
                harness=session.harness,
                session_key=session.session_key,
                workspace=session.workspace,
                request_id=request_id,
                tool=tool,
                detail=detail,
            )
        )
        resolved = await self._wait(request_id, deadline)
        if resolved is None:
            return await self._expire_approval(request_id)
        decision = resolved.get("decision")
        by = str(resolved.get("by", "phone"))
        if decision == "allow_session":
            await self._registry.kv_set(
                ALLOW_SESSION_KEY.format(session_key=session.session_key, tool=tool), "1"
            )
            return Outcome("allow_session", by)
        return Outcome("allow" if decision == "allow" else "deny", by)

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
        resolved = await self._wait(request_id, deadline)
        if resolved is None:
            if await self._service.answer_question(request_id, None, by="deadline"):
                return None, "deadline"
            request = await self._registry.get_pending(request_id)
            resolved = request.answer if request and request.answer else {}
        answer = resolved.get("answer")
        return (answer if isinstance(answer, str) else None), str(resolved.get("by", "phone"))

    async def _wait(self, request_id: str, deadline: datetime) -> dict[str, JsonValue] | None:
        while True:
            request = await self._registry.get_pending(request_id)
            if request is not None and request.status != "open":
                return request.answer or {}
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return None
            await self._signals.wait(pending_key(request_id), min(remaining, 30.0))

    async def _expire_approval(self, request_id: str) -> Outcome:
        if await self._service.answer_approval(request_id, "deny", by="deadline"):
            log.info(f"approval {request_id} expired; denied")
            return Outcome("deny", "deadline")
        request = await self._registry.get_pending(request_id)
        answer = request.answer if request and request.answer else {}
        decisions: dict[str, Decision] = {"allow": "allow", "allow_session": "allow_session"}
        return Outcome(decisions.get(str(answer.get("decision")), "deny"), str(answer.get("by", "phone")))

    # adapter-held requests arrive as events

    async def handle(self, event: Event) -> None:
        if isinstance(event, ApprovalResolved | QuestionAnswered):
            await self._settled_elsewhere(event)
            return
        if not isinstance(event, ApprovalRequested | QuestionAsked):
            return
        if await self._registry.get_pending(event.request_id) is not None:
            return
        payload: dict[str, JsonValue] = {"source": "adapter", "native_id": event.request_id}
        if isinstance(event, ApprovalRequested):
            payload |= {"tool": event.tool, "detail": event.detail}
        else:
            payload |= {"question": event.question}
        await self._registry.add_pending(
            PendingRequest(
                request_id=event.request_id,
                kind="approval" if isinstance(event, ApprovalRequested) else "question",
                session_key=event.session_key,
                payload=payload,
                deadline=datetime.now(UTC) + timedelta(seconds=self._config.deadline_s),
            )
        )

    async def _settled_elsewhere(self, event: ApprovalResolved | QuestionAnswered) -> None:
        """An adapter saw the request answered at the desk: close it without answering natively again.
        Events from our own answers find the request already resolved, so this is idempotent."""
        answer: dict[str, JsonValue]
        if isinstance(event, ApprovalResolved):
            answer = {"decision": event.decision, "by": event.by}
        else:
            answer = {"answer": event.answer, "by": "desk"}
        if await self._registry.resolve_pending(event.request_id, answer):
            self._signals.notify(pending_key(event.request_id))
            log.info(f"{event.request_id} was answered at the desk")

    async def sweep(self) -> int:
        """Deny or skip every open request past its deadline. Returns how many expired."""
        expired = 0
        now = datetime.now(UTC)
        for request in await self._registry.open_pending():
            if request.deadline > now:
                continue
            if request.kind == "approval":
                done = await self._service.answer_approval(request.request_id, "deny", by="deadline")
            else:
                done = await self._service.answer_question(request.request_id, None, by="deadline")
            expired += int(done)
        return expired

    async def sweeper(self, interval: float = 15.0) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("pending sweep failed")
            await asyncio.sleep(interval)
