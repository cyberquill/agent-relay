"""Away mode (one global flag), going back to the desk, and the panic button."""

from __future__ import annotations

from datetime import UTC, datetime

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.registry import GLOBAL_SCOPE
from agent_relay.core.service.approvals import Approvals
from agent_relay.core.service.common import Deps
from agent_relay.core.service.discovery import Discovery
from agent_relay.log import get_logger
from agent_relay.models.capabilities import Capability
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)


class AwayControl:
    def __init__(self, deps: Deps, discovery: Discovery, approvals: Approvals) -> None:
        self._deps = deps
        self._discovery = discovery
        self._approvals = approvals
        # Desk sessions are the phone's business only while away mode is on (see core/away.py).
        deps.router.set_desk_filter(away.DeskGate(deps.registry).visible)

    async def set_away(self, enabled: bool, session_key: str | None = None) -> str:
        """Away mode is global; `session_key` is accepted for old callers and ignored."""
        return await self.set_away_for(enabled)

    async def set_away_for(self, enabled: bool, workspace: str | None = None) -> str:
        """Away on or off for the whole relay (`workspace` is ignored). Off releases parked sessions."""
        await away.set_away(self._deps.registry, enabled)
        handed = 0
        if enabled:
            paths.pause_flag().unlink(missing_ok=True)
        else:
            handed = await self._hand_all()
            await self._release_parked("away mode off")
        reply = f"Away mode {'on' if enabled else 'off'}."
        if handed:
            reply += f" Handed {handed} phone session(s) to the desk."
        return reply

    async def _hand_all(self) -> int:
        count = 0
        for session in await self._deps.registry.list_sessions():
            if await self._hand_to_desk(session):
                count += 1
        return count

    async def _hand_to_desk(self, session: SessionRecord) -> bool:
        """A phone chat the desk can also see becomes a desk session: away mode decides for it from now on."""
        if session.harness not in away.DESK_VISIBLE or not away.phone_held(session):
            return False
        await self._deps.registry.set_session_extra(session.session_key, away.AT_DESK, True)
        log.info(f"handed {session.session_key} to the desk")
        return True

    async def _release_parked(self, reason: str) -> int:
        count = 0
        for session in await self._deps.registry.list_sessions():
            if session.status is not SessionStatus.PARKED:
                continue
            await self.release(session.session_key, reason)
            count += 1
        return count

    async def release(self, session_key: str, reason: str) -> None:
        await self._deps.lifecycle.release(session_key, reason)

    async def back(self, session_key: str | None = None) -> str:
        if session_key is not None:
            session = await self._discovery.session(session_key)
            handed = await self._hand_to_desk(session)
            if session.status is not SessionStatus.PARKED:
                return "Handed to the desk." if handed else "That session is not parked."
            await self.release(session_key, "/back")
            return "Released and handed to the desk." if handed else "Released."
        count = await self._release_parked("/back")
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
        closed = 0
        try:
            requests = await deps.registry.open_pending()
        except Exception:
            log.exception("panic: could not list pending requests")
            requests, failed = [], [*failed, "pending requests"]
        for request in requests:
            if request.kind != "question":
                continue
            try:
                closed += int(await self._approvals.answer_question(request.request_id, None, by="panic"))
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
        summary = f"🛑 Paused. Cancelled {cancelled} turn(s), closed {closed} question(s). `/away` resumes."
        if failed:
            summary += (
                f"\n⚠️ {len(failed)} step(s) failed: {', '.join(failed[:5])}{' …' if len(failed) > 5 else ''}."
            )
        return summary
