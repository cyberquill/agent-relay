"""Answering questions, and telling the adapter and the phone about it.

Tool permissions are never answered here: the relay allows them itself (`core/approvals.py`).
"""

from __future__ import annotations

from agent_relay.core.keys import pending_key
from agent_relay.core.service.common import APP_LABELS, DeliveryError, Deps
from agent_relay.log import get_logger
from agent_relay.models.events import QuestionAnswered, ResolvedBy
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

    async def answer_question(self, request_id: str, answer: str | None, by: ResolvedBy = "phone") -> bool:
        """First answer wins. Returns False when the question was already resolved.

        Raises DeliveryError, with the question open again, when the app that holds it cannot be reached.
        """
        registry = self._deps.registry
        request = await registry.get_pending(request_id)
        if request is None or request.kind != "question":
            return False
        if not await registry.resolve_pending(request_id, {"answer": answer, "by": by}):
            return False
        await self._deliver_or_reopen(request, answer)
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

    async def _deliver_or_reopen(self, request: PendingRequest, answer: str | None) -> None:
        try:
            await self.deliver(request, answer)
        except DeliveryError:
            await self._deps.registry.reopen_pending(request.request_id)
            raise

    async def deliver(self, request: PendingRequest, answer: str | None) -> None:
        """Hook-held questions wake their waiting hook; adapter-held ones are answered natively.

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
            await adapter.answer_question(session, native, answer)
        except Exception as exc:
            log.exception(f"delivering an answer to {session.harness.value} failed")
            raise DeliveryError(_unreachable(label, _reason(exc))) from exc
