"""Answering approvals and questions, and telling the adapter and the phone about it."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, get_args

from pydantic import JsonValue

from agent_relay.adapters.base import Decision
from agent_relay.core import away
from agent_relay.core.keys import pending_key
from agent_relay.core.registry import Registry, stamped_at
from agent_relay.core.service.common import APP_LABELS, DeliveryError, Deps, RelayError
from agent_relay.log import get_logger
from agent_relay.models.events import ApprovalResolved, QuestionAnswered, ResolvedBy, utcnow
from agent_relay.models.sessions import PendingRequest
from agent_relay.scrub import scrub_text

log = get_logger(__name__)

REASON_LIMIT = 120
REASON_MAX_CHARS = 500
DECISIONS: tuple[str, ...] = get_args(Decision)
# `allow_ws:<len>:<workspace>:<tool>` -> expiry time. The length prefix keeps (workspace, tool) unambiguous.
WORKSPACE_GRANT_PREFIX = "allow_ws:"


def clean_reason(text: str | None) -> str | None:
    """A deny reason as the agent will see it: secrets scrubbed, whitespace collapsed, at most 500 chars."""
    if not text:
        return None
    cleaned = " ".join(scrub_text(text).split())[:REASON_MAX_CHARS].rstrip()
    return cleaned or None


def workspace_grant_key(workspace: str, tool: str) -> str:
    root = away.scope_for(workspace)
    return f"{WORKSPACE_GRANT_PREFIX}{len(root)}:{root}:{tool}"


async def record_workspace_grant(
    registry: Registry, workspace: str | None, tool: str | None, seconds: int, now: datetime | None = None
) -> bool:
    """Allow `tool` (exact name) in `workspace` and below for `seconds`; False with nothing to scope."""
    if not workspace or not tool:
        return False
    expires = (now or utcnow()) + timedelta(seconds=seconds)
    await registry.kv_set(workspace_grant_key(workspace, tool), expires.isoformat())
    return True


async def workspace_grant_active(
    registry: Registry, workspace: str | None, tool: str | None, now: datetime | None = None
) -> bool:
    """True when `tool` was granted for this workspace or one of its parent folders and it has not expired."""
    if not workspace or not tool:
        return False
    moment = now or utcnow()
    target = Path(away.scope_for(workspace))
    for folder in (target, *target.parents):
        stored = await registry.kv_get(workspace_grant_key(str(folder), tool))
        expires = stamped_at(stored) if stored else None
        if expires is not None and expires > moment:
            return True
    return False


def _reason(exc: Exception) -> str:
    lines = scrub_text(str(exc)).strip().splitlines()
    text = lines[0].strip() if lines else ""
    return (text or exc.__class__.__name__)[:REASON_LIMIT].rstrip(".")


def _unreachable(label: str, reason: str) -> str:
    return f"Couldn't reach {label}: {reason}. Tap again or answer at the desk."


class Approvals:
    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    async def answer_approval(
        self, request_id: str, decision: Decision, by: ResolvedBy = "phone", reason: str | None = None
    ) -> bool:
        """First answer wins. Returns False when the request was already resolved.

        `reason` only accompanies a deny. `allow_workspace` also records a time-limited grant for the
        request's tool in the session's workspace, once the answer has reached the app.
        Raises DeliveryError, with the request open again, when the app that holds it cannot be reached.
        """
        if decision not in DECISIONS:
            raise RelayError("That is not a decision this request offers.")
        registry = self._deps.registry
        request = await registry.get_pending(request_id)
        if request is None or request.kind != "approval":
            return False
        note = clean_reason(reason) if decision == "deny" else None
        answer: dict[str, JsonValue] = {"decision": decision, "by": by}
        if note:
            answer["reason"] = note
        if not await registry.resolve_pending(request_id, answer):
            return False
        await self._deliver_or_reopen(request, decision=decision, reason=note)
        if decision == "allow_workspace":
            await self._grant_workspace(request)
        await self._announce_approval(request, decision, by)
        return True

    async def _grant_workspace(self, request: PendingRequest) -> None:
        session = await self._deps.registry.get_session(request.session_key)
        tool = request.payload.get("tool")
        seconds = self._deps.config.approvals.workspace_grant_s
        granted = await record_workspace_grant(
            self._deps.registry,
            session.workspace if session else None,
            tool if isinstance(tool, str) else None,
            seconds,
        )
        if not granted:
            log.info(f"{request.request_id}: no workspace or tool to scope a grant to; allowed once")

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
        self,
        request: PendingRequest,
        decision: Decision | None = None,
        answer: str | None = None,
        reason: str | None = None,
    ) -> None:
        try:
            await self.deliver(request, decision=decision, answer=answer, reason=reason)
        except DeliveryError:
            await self._deps.registry.reopen_pending(request.request_id)
            raise

    async def deliver(
        self,
        request: PendingRequest,
        decision: Decision | None = None,
        answer: str | None = None,
        reason: str | None = None,
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
                await adapter.answer_approval(session, native, decision, reason)
            else:
                await adapter.answer_question(session, native, answer)
        except Exception as exc:
            log.exception(f"delivering an answer to {session.harness.value} failed")
            raise DeliveryError(_unreachable(label, _reason(exc))) from exc

    async def _announce_approval(self, request: PendingRequest, decision: Decision, by: ResolvedBy) -> None:
        session = await self._deps.registry.get_session(request.session_key)
        if session is None:
            return
        # The event model has no workspace-grant decision; to every listener it is an allow.
        shown: Literal["allow", "allow_session", "deny"] = (
            "allow" if decision == "allow_workspace" else decision
        )
        await self._deps.router.publish(
            ApprovalResolved(
                harness=session.harness,
                session_key=session.session_key,
                workspace=session.workspace,
                request_id=request.request_id,
                decision=shown,
                by=by,
            )
        )
