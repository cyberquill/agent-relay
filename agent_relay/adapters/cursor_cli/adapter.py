"""Cursor CLI sessions started from the phone: one `cursor-agent acp` process per session.

ACP owns everything for these sessions (text, tools, approvals, questions, plans). A prompt sent
while a turn runs is queued by the core and delivered when the turn ends. When relayd restarts,
the next prompt respawns the process and resumes the conversation with `session/load`.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from agent_relay import paths
from agent_relay.adapters.base import (
    Adapter,
    AdapterUnavailableError,
    Decision,
    EventPublisher,
    ModelInfo,
    StartRequest,
    UnsupportedError,
)
from agent_relay.adapters.cursor_cli import mapping as m
from agent_relay.adapters.cursor_cli.catalog import LOGIN_HINT, CursorCli
from agent_relay.adapters.cursor_cli.rpc import (
    AcpExitedError,
    AcpProcess,
    AcpRpcError,
    MethodNotFoundError,
    NotificationHandler,
    Params,
    RequestHandler,
)
from agent_relay.core.hooks import APPROVED_CALL_KEY
from agent_relay.core.registry import Registry
from agent_relay.log import get_logger
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.events import (
    ApprovalRequested,
    ApprovalResolved,
    ErrorEvent,
    Event,
    Harness,
    Origin,
    QuestionAnswered,
    QuestionAsked,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

type Spawner = Callable[[Sequence[str], Path, RequestHandler, NotificationHandler], Awaitable[AcpProcess]]
type AskKind = Literal["permission", "plan", "question"]

PROTOCOL_VERSION = 1
AUTH_METHOD = "cursor_login"
RESUME_EXTRA = "resume"
INIT_TIMEOUT_S = 60.0
STOPPED_TEXT = "Stopped from the phone."


async def spawn_acp(
    argv: Sequence[str], cwd: Path, on_request: RequestHandler, on_notification: NotificationHandler
) -> AcpProcess:
    return await AcpProcess.spawn(argv, cwd, on_request, on_notification)


def resume_hint(acp_id: str) -> str:
    return f"Cursor CLI. Resume at the desk with `agent resume {acp_id}`"


@dataclass
class _Ask:
    kind: AskKind
    params: Params
    future: asyncio.Future[JsonValue]


@dataclass
class _Live:
    session_key: str
    acp_id: str
    workspace: str
    process: AcpProcess
    set_model: bool
    turn: asyncio.Task[None] | None = None
    text: list[str] = field(default_factory=list)
    asks: dict[str, _Ask] = field(default_factory=dict)
    stale: bool = False

    @property
    def busy(self) -> bool:
        return self.turn is not None and not self.turn.done()


@dataclass
class _Binding:
    """Routes one process's callbacks to its session once `session/new` or `session/load` is done."""

    live: _Live | None = None


class CursorCliAdapter(Adapter):
    harness = Harness.CURSOR_CLI
    capabilities = Capabilities(
        cold_start=True,
        model_select=True,
        token_stream=True,
        approve=True,
        question=True,
        cancel=True,
        wake_idle=True,
    )

    def __init__(
        self,
        registry: Registry,
        cli: CursorCli | None = None,
        spawn: Spawner = spawn_acp,
        init_timeout: float = INIT_TIMEOUT_S,
    ) -> None:
        self._registry = registry
        self._cli = cli or CursorCli(paths.cursor_agent_cli())
        self._spawn = spawn
        self._init_timeout = init_timeout
        self._live: dict[str, _Live] = {}
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._lock = asyncio.Lock()

    # events

    async def run(self, publisher: EventPublisher) -> None:
        while True:
            event = await self._events.get()
            await publisher.publish(event)

    def _emit(self, event: Event) -> None:
        self._events.put_nowait(event)

    async def _session(self, key: str) -> SessionRecord | None:
        return await self._registry.get_session(key)

    # processes

    def argv(self, workspace: str, model: str | None, isolation: bool) -> list[str]:
        argv = [str(self._cli.cli), "--workspace", workspace]
        if model and model != "auto":
            argv += ["--model", model]
        if isolation:
            argv.append("--worktree")
        return [*argv, "acp"]

    async def _call(self, process: AcpProcess, method: str, params: Params) -> Params:
        try:
            result = await process.request(method, params, self._init_timeout)
        except TimeoutError as exc:
            raise AdapterUnavailableError(f"Cursor CLI did not answer `{method}`") from exc
        except AcpRpcError as exc:
            if exc.method == "authenticate" or "auth" in exc.message.lower():
                self._cli.forget_login()
                raise AdapterUnavailableError(LOGIN_HINT) from exc
            raise AdapterUnavailableError(f"Cursor CLI refused `{method}`: {exc.message}") from exc
        return result if isinstance(result, dict) else {}

    async def _open(
        self, workspace: str, model: str | None, isolation: bool, resume: str | None = None
    ) -> tuple[AcpProcess, _Binding, str, bool]:
        """Spawn, initialize, authenticate and create (or load) the ACP session."""
        await self._cli.require_login()
        binding = _Binding()

        async def on_request(method: str, params: Params) -> JsonValue:
            if binding.live is None:
                raise MethodNotFoundError(method)
            return await self._on_request(binding.live, method, params)

        async def on_notification(method: str, params: Params) -> None:
            if binding.live is not None:
                await self._on_notification(binding.live, method, params)

        process = await self._spawn(
            self.argv(workspace, model, isolation), Path(workspace), on_request, on_notification
        )
        try:
            init = await self._call(
                process,
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "agent-relay", "version": "0.1.0"},
                },
            )
            methods = init.get("authMethods")
            offered = (
                [a.get("id") for a in methods if isinstance(a, dict)] if isinstance(methods, list) else []
            )
            if AUTH_METHOD in offered:
                await self._call(process, "authenticate", {"methodId": AUTH_METHOD})
            if resume is None:
                created = await self._call(process, "session/new", {"cwd": workspace, "mcpServers": []})
                acp_id = created.get("sessionId")
                if not isinstance(acp_id, str) or not acp_id:
                    raise AdapterUnavailableError("Cursor CLI returned no session id")
            else:
                caps = init.get("agentCapabilities")
                if not (isinstance(caps, dict) and caps.get("loadSession") is True):
                    raise AdapterUnavailableError(f"{resume_hint(resume)}; this CLI cannot resume over ACP.")
                created = await self._call(
                    process, "session/load", {"sessionId": resume, "cwd": workspace, "mcpServers": []}
                )
                acp_id = resume
        except BaseException:
            await process.close(timeout=2)
            raise
        return process, binding, acp_id, isinstance(created.get("models"), dict)

    def _attach(
        self,
        session_key: str,
        workspace: str,
        process: AcpProcess,
        binding: _Binding,
        acp_id: str,
        set_model: bool,
    ) -> _Live:
        live = _Live(session_key, acp_id, workspace, process, set_model)
        binding.live = live
        self._live[session_key] = live
        return live

    async def _ensure(self, session: SessionRecord) -> _Live:
        """The live process for a session, respawning and resuming it when relayd lost it."""
        async with self._lock:
            live = self._live.get(session.session_key)
            if live is not None and live.process.alive and not live.stale:
                return live
            if live is not None:
                await self._drop(live)
            if not session.workspace:
                raise AdapterUnavailableError(f"{session.session_key} has no workspace to resume in")
            process, binding, acp_id, set_model = await self._open(
                session.workspace, session.model, False, resume=session.harness_id
            )
            log.info(f"resumed Cursor CLI session {acp_id}")
            return self._attach(session.session_key, session.workspace, process, binding, acp_id, set_model)

    async def _drop(self, live: _Live) -> None:
        if self._live.get(live.session_key) is live:
            del self._live[live.session_key]
        self._settle_asks(live)
        await live.process.close(timeout=2)

    # turns

    def _start_turn(self, live: _Live, text: str) -> None:
        live.text = []
        live.turn = asyncio.create_task(self._turn(live, text), name=f"cursor-turn-{live.acp_id}")

    async def _turn(self, live: _Live, text: str) -> None:
        session = await self._session(live.session_key)
        workspace = session.workspace if session else live.workspace
        prompt: Params = {"sessionId": live.acp_id, "prompt": [{"type": "text", "text": text}]}
        try:
            result = await live.process.request("session/prompt", prompt)
        except (AcpRpcError, AcpExitedError) as exc:
            self._settle_asks(live)
            message = exc.message if isinstance(exc, AcpRpcError) else str(exc)
            self._emit(
                ErrorEvent(
                    harness=self.harness, session_key=live.session_key, workspace=workspace, message=message
                )
            )
            await self.report_status(live.session_key, SessionStatus.IDLE, "adapter report")
            if isinstance(exc, AcpExitedError):
                await self._drop(live)
            return
        self._settle_asks(live)
        stop = result.get("stopReason") if isinstance(result, dict) else None
        reply = "".join(live.text).strip() or None
        if stop == "cancelled":
            reply = f"{reply}\n\n{STOPPED_TEXT}" if reply else STOPPED_TEXT
        elif isinstance(stop, str) and stop not in ("end_turn", ""):
            note = f"(Cursor stopped: {stop.replace('_', ' ')})"
            reply = f"{reply}\n\n{note}" if reply else note
        await self.report_status(live.session_key, SessionStatus.IDLE, "adapter report")
        self._emit(
            TurnCompleted(harness=self.harness, session_key=live.session_key, workspace=workspace, text=reply)
        )
        if live.stale:
            await self._drop(live)
            return
        queued = await self._registry.pop_queued(live.session_key, ("steer", "prompt"))
        if queued:
            await self.report_status(live.session_key, SessionStatus.RUNNING, "adapter report")
            self._start_turn(live, "\n\n".join(q.text for q in queued))

    async def _on_notification(self, live: _Live, method: str, params: Params) -> None:
        if method != "session/update":
            log.debug(f"Cursor CLI notification {method} ignored")
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        session = await self._session(live.session_key)
        workspace = session.workspace if session else live.workspace
        base = {"harness": self.harness, "session_key": live.session_key, "workspace": workspace}
        for item in m.map_update(update):
            match item:
                case m.Delta(text=text):
                    live.text.append(text)
                    self._emit(TextDelta(**base, text=text))
                case m.ToolStart(tool_id=tool_id, name=name, detail=detail):
                    self._emit(ToolStarted(**base, tool_id=tool_id, name=name, detail=detail))
                case m.ToolEnd(tool_id=tool_id, name=name, ok=ok, summary=summary):
                    self._emit(ToolFinished(**base, tool_id=tool_id, name=name, ok=ok, summary=summary))

    async def _on_request(self, live: _Live, method: str, params: Params) -> JsonValue:
        if method == "session/request_permission":
            call = params.get("toolCall")
            call_id = call.get("toolCallId") if isinstance(call, dict) else None
            if isinstance(call_id, str) and call_id:
                prior = await self._registry.kv_get(APPROVED_CALL_KEY.format(call_id=call_id))
                if prior in ("allow", "deny"):
                    return m.permission_result(params.get("options"), "allow" if prior == "allow" else "deny")
            tool, detail = m.describe_permission(params)
            decision = await self._ask(live, "permission", params, tool=tool, detail=detail)
            return m.permission_result(params.get("options"), _decision(decision))
        if method == "cursor/create_plan":
            decision = await self._ask(live, "plan", params, tool="plan", detail=m.describe_plan(params))
            return m.plan_result(_decision(decision))
        if method == "cursor/ask_question":
            answer = await self._ask(live, "question", params)
            return m.question_result(params, answer if isinstance(answer, str) else None)
        raise MethodNotFoundError(method)

    async def _ask(
        self, live: _Live, kind: AskKind, params: Params, tool: str = "", detail: str = ""
    ) -> JsonValue:
        request_id = f"cursor-{uuid.uuid4().hex[:12]}"
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        live.asks[request_id] = _Ask(kind, params, future)
        session = await self._session(live.session_key)
        base = {
            "harness": self.harness,
            "session_key": live.session_key,
            "workspace": session.workspace if session else live.workspace,
            "request_id": request_id,
        }
        if kind == "question":
            question, options = m.describe_questions(params)
            self._emit(QuestionAsked(**base, question=question, options=options))
        else:
            self._emit(ApprovalRequested(**base, tool=tool, detail=detail))
        try:
            return await future
        finally:
            live.asks.pop(request_id, None)

    def _settle_asks(self, live: _Live) -> None:
        """Answer every open ask as cancelled (turn over or stopped) and close it on the phone."""
        for request_id, ask in list(live.asks.items()):
            if ask.future.done():
                continue
            ask.future.set_result(None)
            base = {"harness": self.harness, "session_key": live.session_key, "workspace": live.workspace}
            if ask.kind == "question":
                self._emit(QuestionAnswered(**base, request_id=request_id, answer=None))
            else:
                self._emit(ApprovalResolved(**base, request_id=request_id, decision="deny", by="desk"))
        live.asks.clear()

    # the Adapter contract

    async def health(self) -> tuple[bool, str]:
        return await self._cli.status()

    async def list_models(self) -> list[ModelInfo]:
        return await self._cli.models()

    async def start_session(
        self, request: StartRequest, attachments: Sequence[Attachment] = ()
    ) -> SessionRecord:
        request = request.model_copy(update={"prompt": self.prompt_with_files(request.prompt, attachments)})
        process, binding, acp_id, set_model = await self._open(
            request.workspace, request.model, request.isolation
        )
        session_key = f"{self.harness.value}:{acp_id}"
        title = request.prompt.strip().splitlines()[0][:80] if request.prompt.strip() else None
        record = await self._registry.create_session(
            SessionRecord(
                session_key=session_key,
                harness=self.harness,
                harness_id=acp_id,
                workspace=request.workspace,
                model=request.model,
                origin=Origin.PHONE,
                status=SessionStatus.RUNNING,
                title=title,
                extra={RESUME_EXTRA: resume_hint(acp_id)},
            )
        )
        live = self._attach(session_key, request.workspace, process, binding, acp_id, set_model)
        self._start_turn(live, request.prompt)
        log.info(f"started Cursor CLI session {acp_id} in {request.workspace}")
        return record

    async def send_prompt(
        self, session: SessionRecord, text: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        text = self.prompt_with_files(text, attachments)
        live = await self._ensure(session)
        if live.busy:
            raise UnsupportedError(self.harness, "prompting a Cursor CLI turn in progress")
        await self.report_status(session.session_key, SessionStatus.RUNNING, "adapter report")
        self._start_turn(live, text)

    async def wake(self, session: SessionRecord, text: str) -> None:
        await self.send_prompt(session, text)

    async def set_model(self, session: SessionRecord, model: str) -> None:
        known = {info.id for info in await self.list_models()}
        if known and model not in known:
            raise AdapterUnavailableError(f"Cursor CLI has no model `{model}`. Try `/models`.")
        live = self._live.get(session.session_key)
        if live is None:
            return
        if live.set_model:
            try:
                await live.process.request(
                    "session/set_model", {"sessionId": live.acp_id, "modelId": model}, 15
                )
                return
            except (AcpRpcError, TimeoutError) as exc:
                log.info(f"session/set_model failed ({exc}); the process restarts with --model")
        live.stale = True
        if not live.busy:
            await self._drop(live)

    async def cancel(self, session: SessionRecord) -> None:
        live = self._live.get(session.session_key)
        if live is None or not live.busy:
            return
        self._settle_asks(live)
        await live.process.notify("session/cancel", {"sessionId": live.acp_id})

    def _pending_ask(self, session: SessionRecord, request_id: str) -> _Ask | None:
        live = self._live.get(session.session_key)
        ask = live.asks.get(request_id) if live else None
        if ask is None or ask.future.done():
            log.info(f"{request_id} is no longer waiting in Cursor CLI")
            return None
        return ask

    async def answer_approval(self, session: SessionRecord, request_id: str, decision: Decision) -> None:
        ask = self._pending_ask(session, request_id)
        if ask is not None:
            ask.future.set_result(decision)

    async def answer_question(self, session: SessionRecord, request_id: str, answer: str | None) -> None:
        ask = self._pending_ask(session, request_id)
        if ask is not None:
            ask.future.set_result(answer)

    async def aclose(self) -> None:
        for live in list(self._live.values()):
            if live.turn is not None:
                live.turn.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await live.turn
            await self._drop(live)


def _decision(value: JsonValue) -> Decision | None:
    if value == "allow":
        return "allow"
    if value == "allow_session":
        return "allow_session"
    if value == "deny":
        return "deny"
    return None
