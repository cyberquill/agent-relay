"""Away mode, going back to the desk, and the panic button."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.keys import pending_key
from agent_relay.core.registry import GLOBAL_SCOPE
from agent_relay.core.service.approvals import Approvals
from agent_relay.core.service.common import DESK_DECISION, Deps
from agent_relay.core.service.discovery import Discovery
from agent_relay.log import get_logger
from agent_relay.models.capabilities import Capability
from agent_relay.models.events import ApprovalResolved, Origin
from agent_relay.models.sessions import SessionStatus

log = get_logger(__name__)


class AwayControl:
    def __init__(self, deps: Deps, discovery: Discovery, approvals: Approvals) -> None:
        self._deps = deps
        self._discovery = discovery
        self._approvals = approvals

    async def set_away(self, enabled: bool, session_key: str | None = None) -> str:
        workspace = None
        if session_key is not None:
            workspace = (await self._discovery.session(session_key)).workspace
        return await self.set_away_for(enabled, workspace)

    async def set_away_for(self, enabled: bool, workspace: str | None) -> str:
        """Away on/off for one workspace scope (or all); off releases the parked sessions it covers."""
        await away.set_away(self._deps.registry, enabled, workspace)
        if enabled:
            paths.pause_flag().unlink(missing_ok=True)
        else:
            await self._release_matching(workspace, "away mode off")
            await self._return_to_desk()
        scope = f"`{workspace}`" if workspace else "all workspaces"
        return f"Away mode {'on' if enabled else 'off'} for {scope}."

    async def _return_to_desk(self) -> int:
        """Hand hook-held approvals of desk sessions that are no longer away back to the desk.

        Phone sessions keep theirs: nobody is at the desk for them. Adapter-held requests already
        show their own prompt at the desk and are left to it or to the sweeper.
        """
        deps = self._deps
        count = 0
        for request in await deps.registry.open_pending():
            if request.kind != "approval" or request.payload.get("source") == "adapter":
                continue
            session = await deps.registry.get_session(request.session_key)
            if session is None or session.origin is Origin.PHONE:
                continue
            if await away.is_away(deps.registry, session.workspace):
                continue
            answer: dict[str, JsonValue] = {"decision": DESK_DECISION, "by": "desk"}
            if not await deps.registry.resolve_pending(request.request_id, answer):
                continue
            deps.signals.notify(pending_key(request.request_id))
            await deps.router.publish(
                ApprovalResolved(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    request_id=request.request_id,
                    decision="desk",
                    by="desk",
                )
            )
            count += 1
        return count

    async def _release_matching(self, workspace: str | None, reason: str) -> int:
        count = 0
        for session in await self._deps.registry.list_sessions():
            if session.status is not SessionStatus.PARKED:
                continue
            if workspace is not None and away.scope_for(session.workspace) != away.scope_for(workspace):
                continue
            await self.release(session.session_key, reason)
            count += 1
        return count

    async def release(self, session_key: str, reason: str) -> None:
        await self._deps.lifecycle.release(session_key, reason)

    async def back(self, session_key: str | None = None) -> str:
        if session_key is not None:
            session = await self._discovery.session(session_key)
            if session.status is not SessionStatus.PARKED:
                return "That session is not parked."
            await self.release(session_key, "/back")
            return "Released."
        count = await self._release_matching(None, "/back")
        return f"Released {count} parked session(s)."

    async def panic(self) -> str:
        """Pause everything. One failing step never stops the rest; the reply says what failed."""
        deps = self._deps
        failed: list[str] = []
        try:
            paths.pause_flag().parent.mkdir(parents=True, exist_ok=True)
            paths.pause_flag().write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        except OSError:
            log.exception("panic: could not write the pause flag")
            failed.append("pause flag")
        denied = 0
        try:
            requests = await deps.registry.open_pending()
        except Exception:
            log.exception("panic: could not list pending requests")
            requests, failed = [], [*failed, "pending requests"]
        for request in requests:
            try:
                if request.kind == "approval":
                    done = await self._approvals.answer_approval(request.request_id, "deny", by="panic")
                else:
                    done = await self._approvals.answer_question(request.request_id, None, by="panic")
                denied += int(done)
            except Exception:
                log.exception(f"panic: could not answer {request.request_id}")
                failed.append(f"request {request.request_id}")
        cancelled = 0
        try:
            sessions = await deps.registry.list_sessions()
        except Exception:
            log.exception("panic: could not list sessions")
            sessions, failed = [], [*failed, "sessions"]
        for session in sessions:
            settled = True
            adapter = deps.router.adapter(session.harness)
            if (
                session.status is SessionStatus.RUNNING
                and adapter
                and adapter.capabilities.has(Capability.CANCEL)
            ):
                try:
                    await adapter.cancel(session)
                    cancelled += 1
                except Exception:
                    log.exception(f"panic: cancel failed for {session.session_key}")
                    failed.append(f"cancel {session.session_key}")
                    settled = False
            elif session.status is SessionStatus.RUNNING:
                settled = False
            try:
                await deps.lifecycle.stop(session.session_key, "panic", settle=settled)
            except Exception:
                log.exception(f"panic: could not wind down {session.session_key}")
                failed.append(f"wind down {session.session_key}")
        try:
            await deps.registry.set_away(False, GLOBAL_SCOPE)
        except Exception:
            log.exception("panic: could not clear away mode")
            failed.append("away mode")
        deps.signals.notify_all()
        summary = f"🛑 Paused. Cancelled {cancelled} turn(s), denied {denied} request(s). `/away on` resumes."
        if failed:
            summary += (
                f"\n⚠️ {len(failed)} step(s) failed: {', '.join(failed[:5])}{' …' if len(failed) > 5 else ''}."
            )
        return summary
