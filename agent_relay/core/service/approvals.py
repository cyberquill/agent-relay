"""Answering approvals and questions, and telling the adapter and the phone about it."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.adapters.base import Decision
from agent_relay.core.keys import pending_key
from agent_relay.core.service.common import APP_LABELS, DeliveryError, Deps
from agent_relay.log import get_logger
from agent_relay.models.events import ApprovalResolved, QuestionAnswered, ResolvedBy
from agent_relay.models.sessions import PendingRequest
from agent_relay.scrub import scrub_text

log = get_logger(__name__)

REASON_LIMIT = 120


def _reason(exc: Exception) -> str:
    lines = scrub_text(str(exc)).strip().splitlines()
    text = lines[0].strip() if lines else ""
    return (text or exc.__class__.__name__)[:REASON_LIMIT].rstrip(".")


def _unreachable(label: str, reason: str) -> str:
    return f"Couldn't reach {label}: {reason}. Tap again or answer at the desk."


class Approvals:
    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    async def answer_approval(self, request_id: str, decision: Decision, by: ResolvedBy = "phone") -> bool:
        """First answer wins. Returns False when the request was already resolved.

        Raises DeliveryError, with the request open again, when the app that holds it cannot be reached.
        """
        registry = self._deps.registry
        request = await registry.get_pending(request_id)
        if request is None or request.kind != "approval":
            return False
        answer: dict[str, JsonValue] = {"decision": decision, "by": by}
        if not await registry.resolve_pending(request_id, answer):
            return False
        await self._deliver_or_reopen(request, decision=decision)
        await self._announce_approval(request, decision, by)
        return True

    async def answer_question(self, request_id: str, answer: str | None, by: ResolvedBy = "phone") -> bool:
        registry = self._deps.registry
        request = await registry.get_pending(request_id)
        if request is None or request.kind != "question":
            return False
        if not await registry.resolve_pending(request_id, {"answer": answer, "by": by}):
            return False
        await self._deliver_or_reopen(request, answer=answer)
        session = await registry.get_session(request.session_key)
        if session is not None:
            await self._deps.router.publish(
                QuestionAnswered(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    request_id=request_id,
                    answer=answer,
                    by=by,
                )
            )
        return True

    async def _deliver_or_reopen(
        self, request: PendingRequest, decision: Decision | None = None, answer: str | None = None
    ) -> None:
        try:
            await self.deliver(request, decision=decision, answer=answer)
        except DeliveryError:
            await self._deps.registry.reopen_pending(request.request_id)
            raise

    async def deliver(
        self, request: PendingRequest, decision: Decision | None = None, answer: str | None = None
    ) -> None:
        """Hook-held requests wake their waiting hook; adapter-held ones are answered natively.

        Raises DeliveryError when an adapter-held answer does not reach the app.
        """
        deps = self._deps
        deps.signals.notify(pending_key(request.request_id))
        if request.payload.get("source") != "adapter":
            return
        session = await deps.registry.get_session(request.session_key)
        if session is None:
            return
        label = APP_LABELS.get(session.harness, session.harness.value)
        adapter = deps.router.adapter(session.harness)
        if adapter is None:
            raise DeliveryError(_unreachable(label, "it is not connected"))
        native_id = request.payload.get("native_id")
        native = native_id if isinstance(native_id, str) else request.request_id
        try:
            if decision is not None:
                await adapter.answer_approval(session, native, decision)
            else:
                await adapter.answer_question(session, native, answer)
        except Exception as exc:
            log.exception(f"delivering an answer to {session.harness.value} failed")
            raise DeliveryError(_unreachable(label, _reason(exc))) from exc

    async def _announce_approval(self, request: PendingRequest, decision: Decision, by: ResolvedBy) -> None:
        session = await self._deps.registry.get_session(request.session_key)
        if session is None:
            return
        await self._deps.router.publish(
            ApprovalResolved(
                harness=session.harness,
                session_key=session.session_key,
                workspace=session.workspace,
                request_id=request.request_id,
                decision=decision,
                by=by,
            )
        )
