"""The relay's core API. Webex, the PWA and relay-mcp all call this; none talk to adapters directly."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.adapters.base import Adapter, Decision, ModelInfo, StartRequest, UnsupportedError
from agent_relay.core import away, updates
from agent_relay.core.registry import GLOBAL_SCOPE, Registry
from agent_relay.core.router import Router
from agent_relay.core.signals import Signals
from agent_relay.log import get_logger
from agent_relay.models.capabilities import Capability
from agent_relay.models.config import RelayConfig, Workspace
from agent_relay.models.events import (
    ApprovalResolved,
    Harness,
    Origin,
    QuestionAnswered,
    SessionReleased,
    SessionStarted,
)
from agent_relay.models.sessions import PendingRequest, SessionRecord, SessionStatus
from agent_relay.settings import load_workspaces, resolve_workspace

log = get_logger(__name__)

APP_ALIASES: dict[str, Harness] = {
    "copilot": Harness.VSCODE_COPILOT,
    "vscode": Harness.VSCODE_COPILOT,
    "vscode-copilot": Harness.VSCODE_COPILOT,
    "ahp": Harness.VSCODE_COPILOT,
    "local": Harness.VSCODE_LOCAL,
    "vscode-local": Harness.VSCODE_LOCAL,
    "copilot-cli": Harness.COPILOT_CLI,
    "cursor": Harness.CURSOR_CLI,
    "cursor-cli": Harness.CURSOR_CLI,
    "cursor-ide": Harness.CURSOR_CDP,
    "cdp": Harness.CURSOR_CDP,
    "opencode": Harness.OPENCODE,
    "oc": Harness.OPENCODE,
}
APP_LABELS: dict[Harness, str] = {
    Harness.VSCODE_COPILOT: "VS Code · Copilot",
    Harness.VSCODE_LOCAL: "VS Code · Local",
    Harness.COPILOT_CLI: "Copilot CLI",
    Harness.CURSOR: "Cursor IDE (hooks)",
    Harness.CURSOR_CLI: "Cursor CLI",
    Harness.CURSOR_CDP: "Cursor IDE (CDP)",
    Harness.OPENCODE: "OpenCode",
}
STICKY_MODEL_KEY = "model:{harness}"
RELEASE_KEY = "release:{session_key}"
DIFF_LIMIT_BYTES = 5_000_000

type ResolvedBy = Literal["phone", "deadline", "panic", "desk"]


class RelayError(RuntimeError):
    """A user-facing failure: the message is shown as is."""


@dataclass(frozen=True)
class AppInfo:
    harness: Harness
    label: str
    capabilities: tuple[Capability, ...]
    healthy: bool
    detail: str


def pending_key(request_id: str) -> str:
    return f"pending:{request_id}"


def session_signal(session_key: str) -> str:
    return f"session:{session_key}"


def resolve_app(name: str) -> Harness:
    lowered = name.strip().lower()
    if lowered in APP_ALIASES:
        return APP_ALIASES[lowered]
    try:
        return Harness(lowered)
    except ValueError as exc:
        raise RelayError(f"Unknown app `{name}`. Try `/apps`.") from exc


class RelayService:
    def __init__(self, registry: Registry, router: Router, signals: Signals, config: RelayConfig) -> None:
        self.registry = registry
        self.router = router
        self.signals = signals
        self.config = config

    # discovery

    def _adapter(self, harness: Harness) -> Adapter:
        adapter = self.router.adapter(harness)
        if adapter is None:
            raise RelayError(f"{APP_LABELS.get(harness, harness.value)} is not enabled.")
        return adapter

    async def _usable(self, harness: Harness) -> Adapter:
        """The adapter, unless an update made its self-test fail (then the reason is shown)."""
        adapter = self._adapter(harness)
        reason = await updates.degraded_reason(self.registry, harness)
        if reason is not None:
            verb = updates.DEGRADED_VERB.get(harness, "disabled")
            raise RelayError(f"{APP_LABELS.get(harness, harness.value)} is {verb}: {reason}.")
        return adapter

    async def apps(self) -> list[AppInfo]:
        found: list[AppInfo] = []
        for harness, adapter in sorted(self.router.adapters().items(), key=lambda kv: kv[0].value):
            reason = await updates.degraded_reason(self.registry, harness)
            try:
                healthy, detail = await asyncio.wait_for(adapter.health(), 5)
            except Exception as exc:
                healthy, detail = False, f"health check failed: {type(exc).__name__}"
            if reason is not None:
                healthy, detail = False, f"{updates.DEGRADED_VERB.get(harness, 'disabled')}: {reason}"
            found.append(
                AppInfo(
                    harness,
                    APP_LABELS.get(harness, harness.value),
                    adapter.capabilities.supported(),
                    healthy,
                    detail,
                )
            )
        return found

    def workspaces(self) -> list[Workspace]:
        return list(load_workspaces().workspaces)

    async def models(self, app: str) -> list[ModelInfo]:
        harness = resolve_app(app)
        adapter = await self._usable(harness)
        if not adapter.capabilities.has(Capability.MODEL_SELECT):
            raise RelayError(f"{APP_LABELS[harness]} does not offer model selection.")
        return await adapter.list_models()

    async def sessions(self) -> list[SessionRecord]:
        return await self.registry.list_sessions()

    async def session(self, session_key: str) -> SessionRecord:
        found = await self.registry.get_session(session_key)
        if found is None:
            raise RelayError("That session is no longer known to the relay.")
        return found

    # starting and talking

    async def sticky_model(self, harness: Harness) -> str | None:
        return await self.registry.kv_get(STICKY_MODEL_KEY.format(harness=harness.value))

    async def start(self, app: str, workspace: str, prompt: str, model: str | None = None) -> SessionRecord:
        harness = resolve_app(app)
        adapter = await self._usable(harness)
        if not adapter.capabilities.has(Capability.COLD_START):
            raise RelayError(f"{APP_LABELS[harness]} cannot start sessions from the phone.")
        target = resolve_workspace(workspace, load_workspaces())
        if target is None:
            raise RelayError(f"Unknown workspace `{workspace}`. Try `/ws`.")
        chosen = model or await self.sticky_model(harness)
        record = await adapter.start_session(
            StartRequest(workspace=str(paths.expand_user(target.path)), prompt=prompt, model=chosen)
        )
        stored = await self.registry.get_session(record.session_key)
        if stored is not None and stored.origin is Origin.PHONE:
            record = stored
        else:
            record = record.model_copy(update={"origin": Origin.PHONE, "status": SessionStatus.RUNNING})
            record = await self.registry.upsert_session(record)
        if chosen:
            await self.registry.kv_set(STICKY_MODEL_KEY.format(harness=harness.value), chosen)
        await self.router.publish(
            SessionStarted(
                harness=harness,
                session_key=record.session_key,
                workspace=record.workspace,
                origin=Origin.PHONE,
                title=record.title or prompt[:80],
                model=record.model,
            )
        )
        log.info(f"started {harness.value} session {record.session_key}")
        return record

    async def prompt(self, session_key: str, text: str) -> str:
        """Deliver a phone message to a session. Returns a short receipt for the user."""
        session = await self.session(session_key)
        if session.status is SessionStatus.PARKED:
            await self.registry.enqueue(session_key, "prompt", text)
            self.signals.notify(session_signal(session_key))
            return "delivered to the parked session"
        degraded = await updates.degraded_reason(self.registry, session.harness)
        adapter = None if degraded is not None else self.router.adapter(session.harness)
        if session.status is SessionStatus.RUNNING:
            if adapter is not None:
                try:
                    await adapter.send_prompt(session, text)
                    return "sent"
                except UnsupportedError:
                    log.debug(f"{session.harness.value} has no mid-turn prompt; queueing as steering")
            await self.registry.enqueue(session_key, "steer", text)
            return "queued as steering for the next tool call"
        if adapter is not None:
            try:
                await adapter.send_prompt(session, text)
                await self.registry.set_session_fields(session_key, status=SessionStatus.RUNNING.value)
                return "sent"
            except UnsupportedError:
                log.debug(f"{session.harness.value} cannot take a prompt directly; trying wake")
            try:
                await adapter.wake(session, text)
                await self.registry.set_session_fields(session_key, status=SessionStatus.RUNNING.value)
                return "woke the session"
            except UnsupportedError:
                log.debug(f"{session.harness.value} cannot be woken; queueing")
        await self.registry.enqueue(session_key, "prompt", text)
        return "queued; it is delivered the next time the session stops or calls a tool"

    async def set_model(self, session_key: str, model: str) -> None:
        session = await self.session(session_key)
        adapter = await self._usable(session.harness)
        if not adapter.capabilities.has(Capability.MODEL_SELECT):
            raise RelayError(f"{APP_LABELS[session.harness]} does not offer model selection.")
        await adapter.set_model(session, model)
        await self.registry.set_session_fields(session_key, model=model)
        await self.registry.kv_set(STICKY_MODEL_KEY.format(harness=session.harness.value), model)

    async def stop(self, session_key: str) -> None:
        session = await self.session(session_key)
        adapter = self._adapter(session.harness)
        if not adapter.capabilities.has(Capability.CANCEL):
            raise RelayError(f"{APP_LABELS[session.harness]} cannot be cancelled from the phone.")
        await adapter.cancel(session)
        await self.registry.drop_queued(session_key)
        await self.registry.set_session_fields(session_key, status=SessionStatus.IDLE.value)

    async def status(self, session_key: str) -> str:
        session = await self.session(session_key)
        pending = [p for p in await self.registry.open_pending() if p.session_key == session_key]
        is_away = await away.is_away(self.registry, session.workspace)
        lines = [
            f"**{APP_LABELS.get(session.harness, session.harness.value)}** · `{session.workspace or '?'}`",
            f"status: {session.status.value} · model: {session.model or 'default'}"
            f" · away: {'on' if is_away else 'off'}",
            f"pending: {len(pending)} · started from: {session.origin.value}",
        ]
        return "\n".join(lines)

    async def diff(self, session_key: str) -> bytes:
        session = await self.session(session_key)
        if not session.workspace:
            raise RelayError("This session has no workspace.")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            session.workspace,
            "diff",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 30)
        except TimeoutError as exc:
            proc.kill()
            raise RelayError("`git diff` took too long.") from exc
        if proc.returncode != 0:
            raise RelayError(f"`git diff` failed: {err.decode(errors='replace').strip()[:200]}")
        if len(out) > DIFF_LIMIT_BYTES:
            raise RelayError("The diff is larger than 5 MB.")
        return out

    # away, back, panic

    async def set_away(self, enabled: bool, session_key: str | None = None) -> str:
        workspace = None
        if session_key is not None:
            workspace = (await self.session(session_key)).workspace
        return await self.set_away_for(enabled, workspace)

    async def set_away_for(self, enabled: bool, workspace: str | None) -> str:
        """Away on/off for one workspace scope (or all); off releases the parked sessions it covers."""
        await away.set_away(self.registry, enabled, workspace)
        if enabled:
            paths.pause_flag().unlink(missing_ok=True)
        else:
            await self._release_matching(workspace, "away mode off")
        scope = f"`{workspace}`" if workspace else "all workspaces"
        return f"Away mode {'on' if enabled else 'off'} for {scope}."

    async def _release_matching(self, workspace: str | None, reason: str) -> int:
        count = 0
        for session in await self.registry.list_sessions():
            if session.status is not SessionStatus.PARKED:
                continue
            if workspace is not None and away.scope_for(session.workspace) != away.scope_for(workspace):
                continue
            await self.release(session.session_key, reason)
            count += 1
        return count

    async def release(self, session_key: str, reason: str) -> None:
        await self.registry.kv_set(RELEASE_KEY.format(session_key=session_key), reason)
        self.signals.notify(session_signal(session_key))
        session = await self.registry.get_session(session_key)
        if session is not None:
            await self.router.publish(
                SessionReleased(
                    harness=session.harness,
                    session_key=session_key,
                    workspace=session.workspace,
                    reason=reason,
                )
            )

    async def back(self, session_key: str | None = None) -> str:
        if session_key is not None:
            session = await self.session(session_key)
            if session.status is not SessionStatus.PARKED:
                return "That session is not parked."
            await self.release(session_key, "/back")
            return "Released."
        count = await self._release_matching(None, "/back")
        return f"Released {count} parked session(s)."

    async def panic(self) -> str:
        paths.pause_flag().parent.mkdir(parents=True, exist_ok=True)
        paths.pause_flag().write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        denied = 0
        for request in await self.registry.open_pending():
            if request.kind == "approval":
                if await self.answer_approval(request.request_id, "deny", by="panic"):
                    denied += 1
            elif await self.answer_question(request.request_id, None, by="panic"):
                denied += 1
        cancelled = 0
        for session in await self.registry.list_sessions():
            if session.status is SessionStatus.PARKED:
                await self.release(session.session_key, "panic")
            adapter = self.router.adapter(session.harness)
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
            await self.registry.drop_queued(session.session_key)
        await self.registry.set_away(False, GLOBAL_SCOPE)
        self.signals.notify_all()
        return f"🛑 Paused. Cancelled {cancelled} turn(s), denied {denied} request(s). `/away on` resumes."

    # approvals and questions

    async def answer_approval(self, request_id: str, decision: Decision, by: ResolvedBy = "phone") -> bool:
        """First answer wins. Returns False when the request was already resolved."""
        request = await self.registry.get_pending(request_id)
        if request is None or request.kind != "approval":
            return False
        answer: dict[str, JsonValue] = {"decision": decision, "by": by}
        if not await self.registry.resolve_pending(request_id, answer):
            return False
        await self._deliver(request, decision=decision)
        await self._announce_approval(request, decision, by)
        return True

    async def answer_question(self, request_id: str, answer: str | None, by: ResolvedBy = "phone") -> bool:
        request = await self.registry.get_pending(request_id)
        if request is None or request.kind != "question":
            return False
        if not await self.registry.resolve_pending(request_id, {"answer": answer, "by": by}):
            return False
        await self._deliver(request, answer=answer)
        session = await self.registry.get_session(request.session_key)
        if session is not None:
            await self.router.publish(
                QuestionAnswered(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    request_id=request_id,
                    answer=answer,
                )
            )
        return True

    async def _deliver(
        self, request: PendingRequest, decision: Decision | None = None, answer: str | None = None
    ) -> None:
        """Hook-held requests wake their waiting hook; adapter-held ones are answered natively."""
        self.signals.notify(pending_key(request.request_id))
        if request.payload.get("source") != "adapter":
            return
        session = await self.registry.get_session(request.session_key)
        if session is None:
            return
        adapter = self.router.adapter(session.harness)
        if adapter is None:
            return
        native_id = request.payload.get("native_id")
        native = native_id if isinstance(native_id, str) else request.request_id
        try:
            if decision is not None:
                await adapter.answer_approval(session, native, decision)
            else:
                await adapter.answer_question(session, native, answer)
        except Exception:
            log.exception(f"delivering an answer to {session.harness.value} failed")

    async def _announce_approval(self, request: PendingRequest, decision: Decision, by: ResolvedBy) -> None:
        session = await self.registry.get_session(request.session_key)
        if session is None:
            return
        await self.router.publish(
            ApprovalResolved(
                harness=session.harness,
                session_key=session.session_key,
                workspace=session.workspace,
                request_id=request.request_id,
                decision=decision,
                by=by,
            )
        )
