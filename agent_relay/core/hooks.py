"""relayd's side of relay-hook: normalise, decide, and answer in the harness's own format.

Nothing happens while away mode is off for the workspace (no noise at the desk): every event gets
an empty reply and the harness carries on as if the relay were not installed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime

from pydantic import JsonValue

from agent_relay.core import away
from agent_relay.core.approvals import CLOSED_REASONS, DEADLINE_REASON, ApprovalBroker
from agent_relay.core.claims import UNCLAIMED_PREFIX, aliased, link_alias, take_claim
from agent_relay.core.lifecycle import Lifecycle
from agent_relay.core.parking import ParkManager, phone_text
from agent_relay.core.registry import RECENT_APPROVAL_PREFIX, RECENT_APPROVAL_TTL, Registry, stamped_at
from agent_relay.core.router import Router
from agent_relay.hook import outputs, transcript
from agent_relay.hook.shims import (
    HookFamily,
    HookInput,
    HookKind,
    UnknownHookEventError,
    describe_tool,
    normalize,
)
from agent_relay.log import get_logger
from agent_relay.models.events import (
    ErrorEvent,
    Harness,
    Origin,
    SessionReleased,
    SessionStarted,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    utcnow,
)
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

type Reply = dict[str, JsonValue]

APPROVAL_KINDS = frozenset(
    {HookKind.PRE_TOOL, HookKind.PERMISSION, HookKind.SHELL_BEFORE, HookKind.MCP_BEFORE}
)
AFTER_KINDS = frozenset({HookKind.POST_TOOL, HookKind.SHELL_AFTER, HookKind.MCP_AFTER, HookKind.TOOL_FAILURE})
COPILOT_FAMILY = (Harness.VSCODE_COPILOT, Harness.VSCODE, Harness.COPILOT_CLI)
CURSOR_FAMILY = (Harness.CURSOR, Harness.CURSOR_CLI)
ACP_OWNED = frozenset({Harness.CURSOR_CLI})
CURSOR_GATED = ("shell", "terminal", "mcp")
RECENT_APPROVAL_KEY = f"{RECENT_APPROVAL_PREFIX}{{session_key}}:{{digest}}"
APPROVED_CALL_KEY = "approved_call:{call_id}"
OPENCODE_SERVER_PREFIX = "opencode.server:"
LAST_TEXT = "last_text"
RENEWING = "renewing"
HOOKED = "hooked"


def _reply(stdout: dict[str, JsonValue] | None) -> Reply:
    return {} if not stdout else {"stdout": stdout, "exit": 0}


def _digest(hook: HookInput) -> str:
    raw = json.dumps([hook.session_id, hook.tool_name, hook.tool_input], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class HookService:
    def __init__(
        self,
        registry: Registry,
        router: Router,
        broker: ApprovalBroker,
        parking: ParkManager,
        lifecycle: Lifecycle,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._registry = registry
        self._router = router
        self._broker = broker
        self._parking = parking
        self._lifecycle = lifecycle
        self._clock = clock

    async def handle(self, message: dict[str, object]) -> Reply:
        family, event, payload = message.get("harness"), message.get("event"), message.get("payload")
        if not isinstance(family, str) or not isinstance(event, str) or not isinstance(payload, dict):
            return {}
        try:
            hook = normalize(family, event, payload)
        except (UnknownHookEventError, ValueError):
            log.debug(f"unhandled hook {family}/{event}")
            return {}
        if hook.kind is HookKind.PLUGIN_REGISTER:
            if hook.server_url:
                await self._registry.kv_set(
                    f"{OPENCODE_SERVER_PREFIX}{hook.server_url}", hook.workspace or ""
                )
            return {}
        session = await self._existing(hook)
        if session is not None and session.harness in ACP_OWNED:
            return {}
        phone_session = session is not None and session.origin is Origin.PHONE
        workspace = hook.workspace or (session.workspace if session else None)
        if not phone_session and not await away.is_away(self._registry, workspace):
            if session is not None and hook.kind is HookKind.PROMPT_SUBMIT:
                await self._parking.reset_blocks(session.session_key)
            return {}
        if session is None:
            if hook.kind in (HookKind.SESSION_END, HookKind.SUBAGENT_STOP):
                return {}
            session = await self._register(hook)
        if session.status is SessionStatus.ENDED and hook.kind is not HookKind.SESSION_END:
            session = (
                await self._lifecycle.observe(session.session_key, SessionStatus.RUNNING, "hook activity")
                or session
            )
        if session.extra.get(HOOKED) is not True:
            await self._registry.set_session_extra(session.session_key, HOOKED, True)
        try:
            return await self._dispatch(session, hook)
        except Exception:
            log.exception(f"hook {hook.family.value}/{hook.event} failed")
            if hook.kind in APPROVAL_KINDS:
                return _reply(
                    outputs.permission(hook, False, "agent-relay failed while asking the phone; denied.")
                )
            return {}

    # sessions

    async def _existing(self, hook: HookInput) -> SessionRecord | None:
        if hook.family is HookFamily.COPILOT:
            candidates: tuple[Harness, ...] = COPILOT_FAMILY
        elif hook.family is HookFamily.CURSOR:
            candidates = CURSOR_FAMILY
        else:
            candidates = (hook.harness,)
        for harness in candidates:
            found = await self._registry.find_session(harness, hook.session_id)
            if found is not None:
                return found
        if hook.family is HookFamily.COPILOT:
            key = await aliased(self._registry, hook.session_id)
            found = await self._registry.get_session(key) if key else None
            if found is not None:
                return found
        return await self._claimed(hook)

    async def _claimed(self, hook: HookInput) -> SessionRecord | None:
        """Bind the first unmatched VS Code hook in a workspace to the chat the phone just opened there."""
        vscode = hook.family is HookFamily.COPILOT and hook.event[:1].isupper()
        if not vscode or hook.kind is HookKind.SESSION_END or hook.session_id == "unknown":
            return None
        claim = await take_claim(self._registry, hook.workspace)
        if claim is None:
            return None
        session = await self._registry.get_session(claim.session_key)
        if session is None:
            return None
        if session.harness_id.startswith(UNCLAIMED_PREFIX):
            await self._registry.update_session_fields(session.session_key, harness_id=hook.session_id)
            log.info(f"linked {session.session_key} to VS Code chat {hook.session_id}")
            return await self._registry.get_session(session.session_key)
        if session.harness is Harness.VSCODE_COPILOT:
            await link_alias(self._registry, hook.session_id, session.session_key)
            log.info(f"linked {session.session_key} to Agent Host hook session {hook.session_id}")
            return session
        return None

    async def _register(self, hook: HookInput) -> SessionRecord:
        record = await self._registry.create_session(
            SessionRecord(
                session_key=f"{hook.harness.value}:{hook.session_id}",
                harness=hook.harness,
                harness_id=hook.session_id,
                workspace=hook.workspace,
                origin=Origin.DESK,
                status=SessionStatus.RUNNING,
                title=(hook.text or "")[:80] or None,
            )
        )
        await self._router.publish(
            SessionStarted(
                harness=record.harness,
                session_key=record.session_key,
                workspace=record.workspace,
                title=record.title,
            )
        )
        log.info(f"registered {record.session_key} from a {hook.event} hook")
        return record

    # dispatch

    async def _dispatch(self, session: SessionRecord, hook: HookInput) -> Reply:
        kind = hook.kind
        if kind in APPROVAL_KINDS:
            return await self._approve(session, hook)
        if kind in AFTER_KINDS:
            return await self._after_tool(session, hook)
        if kind is HookKind.STOP:
            return await self._stop(session, hook)
        if kind is HookKind.PROMPT_SUBMIT:
            await self._lifecycle.observe(
                session.session_key, SessionStatus.RUNNING, f"{hook.kind.value} hook"
            )
            await self._parking.reset_blocks(session.session_key)
            return {}
        if kind is HookKind.AGENT_RESPONSE and hook.text:
            await self._registry.set_session_extra(session.session_key, LAST_TEXT, hook.text)
            return {}
        if kind is HookKind.FILE_EDIT:
            await self._publish_tool(session, hook, "edit", started=False)
            return {}
        if kind is HookKind.NOTIFICATION:
            return await self._notification(session, hook)
        if kind is HookKind.SESSION_END:
            if session.harness is Harness.VSCODE_COPILOT:
                return {}
            await self._lifecycle.observe(session.session_key, SessionStatus.ENDED, "session ended")
            await self._router.publish(
                SessionReleased(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    reason="session ended",
                )
            )
            return {}
        if kind is HookKind.ERROR and hook.error:
            await self._router.publish(
                ErrorEvent(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    message=hook.error,
                )
            )
        return {}

    async def _publish_tool(
        self, session: SessionRecord, hook: HookInput, name: str | None, started: bool
    ) -> None:
        tool_id = hook.tool_use_id or _digest(hook)
        label = name or hook.tool_name or hook.kind.value
        if started:
            detail = describe_tool(hook, 200).partition(": ")[2]
            await self._router.publish(
                ToolStarted(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    tool_id=tool_id,
                    name=label,
                    detail=detail,
                )
            )
        else:
            failed = hook.kind is HookKind.TOOL_FAILURE
            summary = (hook.error or hook.tool_output or "")[:200] if failed else ""
            await self._router.publish(
                ToolFinished(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    tool_id=tool_id,
                    name=label,
                    ok=not failed,
                    summary=summary,
                )
            )

    async def _approve(self, session: SessionRecord, hook: HookInput) -> Reply:
        tool = hook.tool_name or hook.kind.value
        if hook.family is HookFamily.CURSOR and hook.kind is HookKind.PRE_TOOL:
            if any(word in tool.lower() for word in CURSOR_GATED):
                return {}
        await self._publish_tool(session, hook, None, started=True)
        if self._broker.auto_allowed(tool):
            return _reply(outputs.permission(hook, True, "read-only tool"))
        recent = RECENT_APPROVAL_KEY.format(session_key=session.session_key, digest=_digest(hook))
        if hook.kind is HookKind.PERMISSION and await self._recently_allowed(recent):
            return _reply(outputs.permission(hook, True, "approved from the phone"))
        outcome = await self._broker.request(session, tool, describe_tool(hook))
        if outcome.deferred:
            return {}
        decision = "allow" if outcome.allowed else "deny"
        await self._registry.kv_set(recent, f"{self._clock().isoformat()} {decision}")
        if hook.tool_use_id:
            await self._registry.kv_set(APPROVED_CALL_KEY.format(call_id=hook.tool_use_id), decision)
        if outcome.allowed:
            return _reply(outputs.permission(hook, True, "approved from the phone"))
        reason = (
            DEADLINE_REASON
            if outcome.by == "deadline"
            else CLOSED_REASONS.get(outcome.by, "Denied from the phone by the user.")
        )
        return _reply(outputs.permission(hook, False, reason))

    async def _recently_allowed(self, key: str) -> bool:
        """True only for an allow stamped within the last RECENT_APPROVAL_TTL."""
        stored = await self._registry.kv_get(key)
        if stored is None:
            return False
        stamped = stamped_at(stored)
        decision = stored.partition(" ")[2]
        return stamped is not None and decision == "allow" and self._clock() - stamped < RECENT_APPROVAL_TTL

    async def _after_tool(self, session: SessionRecord, hook: HookInput) -> Reply:
        await self._publish_tool(session, hook, None, started=False)
        steerable = hook.kind in (HookKind.POST_TOOL, HookKind.TOOL_FAILURE)
        if not steerable:
            return {}
        queued = await self._registry.pop_queued(session.session_key, ("steer", "prompt"))
        if not queued:
            return {}
        return _reply(outputs.add_context(hook, phone_text([q.text for q in queued])))

    async def _notification(self, session: SessionRecord, hook: HookInput) -> Reply:
        if hook.notification_type != "agent_idle":
            return {}
        queued = await self._registry.pop_queued(session.session_key, ("prompt", "steer"))
        if not queued:
            return {}
        await self._lifecycle.observe(session.session_key, SessionStatus.RUNNING, f"{hook.kind.value} hook")
        return _reply(outputs.add_context(hook, phone_text([q.text for q in queued])))

    async def _final_text(self, session: SessionRecord, hook: HookInput) -> str | None:
        if hook.family is HookFamily.CURSOR:
            text = session.extra.get(LAST_TEXT)
            await self._registry.set_session_extra(session.session_key, LAST_TEXT, None)
            return text if isinstance(text, str) else None
        return transcript.last_reply(hook.transcript_path)

    async def _stop(self, session: SessionRecord, hook: HookInput) -> Reply:
        text = await self._final_text(session, hook)
        if session.extra.get(RENEWING) is not True:
            await self._router.publish(
                TurnCompleted(
                    harness=session.harness,
                    session_key=session.session_key,
                    workspace=session.workspace,
                    text=text,
                )
            )
            await self._router.join()
        current = await self._registry.get_session(session.session_key) or session
        result = await self._parking.park(current, hook.stop_hook_active)
        await self._registry.set_session_extra(session.session_key, RENEWING, result.kind == "renewed")
        if result.blocks:
            return _reply(outputs.continue_with(hook, result.text))
        return {}
