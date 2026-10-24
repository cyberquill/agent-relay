"""AHP adapter: the VS Code Copilot harness through the editor's own Agent Host socket.

Sessions started from the phone run here end to end. Desk sessions are picked up when the hooks
already know them (the Copilot CLI hook session id is the AHP session id) or, with away mode on,
when they become active. For sessions the hooks also report, only reply text streams from here;
tool events, the final answer and parking stay with the hooks so nothing reaches the phone twice.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from agent_relay import __version__
from agent_relay.adapters.ahp import mapping as mp
from agent_relay.adapters.ahp import models as m
from agent_relay.adapters.ahp import uris
from agent_relay.adapters.ahp.endpoints import SUPPORTED_VERSIONS, AhpUnavailableError, Endpoint, discover
from agent_relay.adapters.ahp.rpc import AhpRpcError, Connection, Notification, initialize
from agent_relay.adapters.base import (
    Adapter,
    AdapterUnavailableError,
    Decision,
    EventPublisher,
    ModelInfo,
    StartRequest,
    UnsupportedError,
)
from agent_relay.core import away
from agent_relay.core.claims import make_claim
from agent_relay.core.hooks import APPROVED_CALL_KEY, HOOKED
from agent_relay.core.registry import Registry
from agent_relay.core.service import RelayError
from agent_relay.log import get_logger
from agent_relay.models.attachments import Attachment
from agent_relay.models.capabilities import Capabilities, Capability
from agent_relay.models.events import (
    ApprovalRequested,
    ApprovalResolved,
    ErrorEvent,
    Event,
    Harness,
    Origin,
    QuestionAnswered,
    QuestionAsked,
    SessionStarted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from agent_relay.models.sessions import SessionRecord, SessionStatus

log = get_logger(__name__)

PROVIDER = "copilotcli"
CLIENT_NAME = "agent-relay"
SESSION_EXTRA = "ahp_session"
CHAT_EXTRA = "ahp_chat"
TURN_EXTRA = "ahp_turn"
PROVIDER_EXTRA = "ahp_provider"
IN_PROGRESS = 8
READY_TIMEOUT_S = 10.0
MAX_LIST_PAGES = 50
DEFAULT_MODEL = "auto"

type Sleep = Callable[[float], Awaitable[None]]
type Discover = Callable[[], Awaitable[Endpoint]]
type Connect = Callable[[Endpoint], Awaitable[Connection]]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _message_attachment(
    item: Attachment,
) -> m.MessageEmbeddedResourceAttachment | m.MessageResourceAttachment:
    """Images travel inline, like a pasted image; anything else is a file URI the host reads itself."""
    if item.mime.startswith("image/"):
        try:
            data = await asyncio.to_thread(Path(item.local_path).read_bytes)
        except OSError:
            log.warning(f"cannot read {item.name}; sending it as a file reference")
        else:
            return m.MessageEmbeddedResourceAttachment(
                label=item.name,
                display_kind="image",
                data=base64.b64encode(data).decode(),
                content_type=item.mime,
            )
    return m.MessageResourceAttachment(
        label=item.name,
        display_kind="document",
        uri=uris.file_uri(item.local_path),
        size_hint=item.size,
        content_type=item.mime,
    )


class AhpAdapter(Adapter):
    harness = Harness.VSCODE_COPILOT
    capabilities = Capabilities(
        cold_start=True,
        model_select=True,
        token_stream=True,
        approve=True,
        question=True,
        cancel=True,
        wake_idle=True,
        attach_files=True,
    )

    def __init__(
        self,
        registry: Registry,
        discover_endpoint: Discover = discover,
        connect: Connect = Connection.open,
        sleep: Sleep = asyncio.sleep,
        backoff: tuple[float, float] = (1.0, 60.0),
    ) -> None:
        self._registry = registry
        self._discover = discover_endpoint
        self._connect = connect
        self._sleep = sleep
        self._backoff = backoff
        self._client_id = f"{CLIENT_NAME}-{uuid.uuid4()}"
        self._conn: Connection | None = None
        self._ready = asyncio.Event()
        self._root: m.RootState | None = None
        self._version: str | None = None
        self._seq = itertools.count(1)
        self._subscribed: set[str] = set()
        self._mapper = mp.ActionMapper()
        self._asks: dict[str, mp.ToolAsk] = {}
        self._inputs: dict[str, mp.InputAsk] = {}
        self._turn_started: dict[str, float] = {}
        self._publisher: EventPublisher | None = None

    # connection

    @property
    def connected(self) -> bool:
        return self._ready.is_set() and self._conn is not None and not self._conn.closed

    async def run(self, publisher: EventPublisher) -> None:
        self._publisher = publisher
        low, high = self._backoff
        delay = low
        while True:
            try:
                conn = await self._open()
                delay = low
                await self._pump(conn)
            except asyncio.CancelledError:
                raise
            except AhpUnavailableError as exc:
                log.debug(f"agent host unavailable: {exc}")
            except Exception:
                log.exception("agent host connection failed")
            finally:
                await self._drop()
            await self._sleep(delay)
            delay = min(delay * 2, high)

    async def _open(self) -> Connection:
        endpoint = await self._discover()
        conn = await self._connect(endpoint)
        self._conn = conn
        params = m.InitializeParams(
            channel=uris.ROOT,
            protocol_versions=list(SUPPORTED_VERSIONS),
            client_id=self._client_id,
            client_info=m.Implementation(name=CLIENT_NAME, version=__version__),
            initial_subscriptions=[uris.ROOT],
        )
        result = await initialize(conn, endpoint, params)
        self._version = result.protocol_version
        for snapshot in result.snapshots:
            if isinstance(snapshot.state, m.RootState):
                self._root = snapshot.state
        self._subscribed = {uris.ROOT}
        await self.refresh_sessions(reconcile=True)
        self._ready.set()
        log.info(f"connected to the VS Code Agent Host (AHP {result.protocol_version})")
        return conn

    async def _drop(self) -> None:
        self._ready.clear()
        conn, self._conn = self._conn, None
        self._subscribed.clear()
        self._asks.clear()
        self._inputs.clear()
        self._turn_started.clear()
        self._mapper = mp.ActionMapper()
        if conn is not None:
            await conn.close()

    async def _pump(self, conn: Connection) -> None:
        async for note in conn.notifications():
            try:
                await self.consume(note)
            except AhpUnavailableError:
                raise
            except Exception:
                log.exception(f"agent host {note.method} handling failed")

    async def _live(self) -> Connection:
        try:
            await asyncio.wait_for(self._ready.wait(), READY_TIMEOUT_S)
        except TimeoutError as exc:
            raise AdapterUnavailableError("VS Code's Agent Host is not connected (is VS Code open?)") from exc
        if self._conn is None or self._conn.closed:
            raise AdapterUnavailableError("VS Code's Agent Host connection dropped; retrying")
        return self._conn

    async def _subscribe(self, conn: Connection, channel: str) -> None:
        if channel in self._subscribed:
            return
        await conn.request("subscribe", m.SubscribeParams(channel=channel))
        self._subscribed.add(channel)

    async def _unsubscribe(self, conn: Connection, channel: str) -> None:
        if channel == uris.ROOT or channel not in self._subscribed:
            return
        self._subscribed.discard(channel)
        try:
            await conn.notify("unsubscribe", m.UnsubscribeParams(channel=channel))
        except AhpUnavailableError as exc:
            log.debug(f"could not unsubscribe from {channel}: {exc}")

    async def _release(self, conn: Connection | None, record: SessionRecord) -> None:
        """Stop listening to a session the relay is done with, and forget its in-memory turn state."""
        channels = [record.extra.get(SESSION_EXTRA), record.extra.get(CHAT_EXTRA)]
        for channel in channels:
            if not isinstance(channel, str):
                continue
            if conn is not None:
                await self._unsubscribe(conn, channel)
            self._mapper.forget(channel)
            for asks in (self._asks, self._inputs):
                for call_id in [k for k, ask in asks.items() if ask.chat == channel]:
                    del asks[call_id]
        self._turn_started.pop(record.session_key, None)

    async def release_session(self, session: SessionRecord) -> None:
        """Called when a session ends or is dropped from the relay's tracking."""
        await self._release(self._conn, session)

    async def _ensure_subscribed(self, conn: Connection, record: SessionRecord) -> None:
        for channel in (record.extra.get(SESSION_EXTRA), record.extra.get(CHAT_EXTRA)):
            if isinstance(channel, str):
                await self._subscribe(conn, channel)

    async def _dispatch(self, conn: Connection, chat: str, action: m.AhpModel) -> None:
        params = m.DispatchActionParams(channel=chat, client_seq=next(self._seq), action=action.wire())
        await conn.notify("dispatchAction", params)

    # session discovery

    async def _known(self, sid: str) -> SessionRecord | None:
        for harness in (Harness.VSCODE_COPILOT, Harness.COPILOT_CLI):
            found = await self._registry.find_session(harness, sid)
            if found is not None:
                return found
        return None

    async def list_host_sessions(self) -> list[m.SessionSummary]:
        conn = self._conn
        if conn is None:
            raise AdapterUnavailableError("VS Code's Agent Host is not connected")
        return (await self._list_pages(conn))[0]

    async def _list_pages(self, conn: Connection) -> tuple[list[m.SessionSummary], bool]:
        """Every session summary, following `nextCursor`. The flag is False when the page cap cut it short."""
        found: list[m.SessionSummary] = []
        cursor: str | None = None  # opaque and per connection: never kept past this call
        for _ in range(MAX_LIST_PAGES):
            raw = await conn.request("listSessions", m.ListSessionsParams(channel=uris.ROOT, cursor=cursor))
            items = raw.get("items") if isinstance(raw, dict) else None
            for item in items if isinstance(items, list) else []:
                try:
                    found.append(m.SessionSummary.model_validate(item))
                except ValidationError:
                    log.debug("skipped a session summary this relay cannot read")
            following = raw.get("nextCursor") if isinstance(raw, dict) else None
            if not isinstance(following, str) or not following:
                return found, True
            cursor = following
        log.warning(f"the Agent Host session list passed {MAX_LIST_PAGES} pages; the rest is ignored")
        return found, False

    async def refresh_sessions(self, *, reconcile: bool = False) -> None:
        """Adopt host sessions the relay knows, and active desk sessions while away mode is on.

        `reconcile` is for a fresh connection: a turn the relay still tracks that the host no longer
        runs is cleared, since nothing will ever finish it.
        """
        conn = self._conn
        if conn is None:
            return
        summaries, complete = await self._list_pages(conn)
        listed: set[str] = set()
        for summary in summaries:
            sid = uris.session_id(summary.resource)
            if sid is None:
                continue
            listed.add(sid)
            record = await self._known(sid)
            workspace = next((p for p in map(uris.file_path, summary.working_directories or []) if p), None)
            if record is None:
                active = summary.status & IN_PROGRESS
                if not active or not await away.is_away(self._registry, workspace):
                    continue
                record = await self._adopt_desk(summary, sid, workspace)
            if reconcile and not summary.status & IN_PROGRESS:
                await self._clear_turn(record)
            await self._attach(conn, record, summary)
        for record in await self._registry.list_sessions(include_ended=True):
            if record.harness is not self.harness:
                continue
            if reconcile and complete and record.harness_id not in listed:
                await self._clear_turn(record)
            if record.status is SessionStatus.ENDED:
                await self._release(conn, record)

    async def _clear_turn(self, record: SessionRecord) -> None:
        """Forget a turn the host no longer has, and settle a session that was only running for it."""
        if not isinstance(record.extra.get(TURN_EXTRA), str):
            return
        await self._registry.set_session_extra(record.session_key, TURN_EXTRA, None)
        self._turn_started.pop(record.session_key, None)
        if record.extra.get(HOOKED) is not True and record.status is SessionStatus.RUNNING:
            await self.report_status(record.session_key, SessionStatus.IDLE, "adapter report")
        log.info(f"{record.session_key} lost its turn when the Agent Host connection dropped")

    async def _adopt_desk(self, summary: m.SessionSummary, sid: str, workspace: str | None) -> SessionRecord:
        record = await self._registry.create_session(
            SessionRecord(
                session_key=f"{self.harness.value}:{sid}",
                harness=self.harness,
                harness_id=sid,
                workspace=workspace,
                title=summary.title or None,
                status=SessionStatus.RUNNING,
                origin=Origin.DESK,
            )
        )
        await self._emit(SessionStarted, record, title=record.title)
        return record

    async def _attach(self, conn: Connection, record: SessionRecord, summary: m.SessionSummary) -> None:
        if record.harness is Harness.COPILOT_CLI:
            await self._registry.update_session_fields(record.session_key, harness=self.harness.value)
            log.info(f"{record.session_key} runs in VS Code; the Agent Host now serves it")
        chat = record.extra.get(CHAT_EXTRA)
        if not isinstance(chat, str):
            chat = summary.default_chat or uris.chat_uri(uris.DEFAULT_CHAT, summary.resource)
            await self._registry.set_session_extra(record.session_key, CHAT_EXTRA, chat)
            await self._registry.set_session_extra(record.session_key, SESSION_EXTRA, summary.resource)
        if record.status is SessionStatus.ENDED:
            return
        await self._subscribe(conn, summary.resource)
        await self._subscribe(conn, chat)

    # notifications

    async def consume(self, note: Notification) -> None:
        if note.method != "action":
            return
        channel, action = note.params.get("channel"), note.params.get("action")
        if not isinstance(channel, str) or not isinstance(action, dict):
            return
        if channel == uris.ROOT:
            if action.get("type") == "root/activeSessionsChanged":
                await self.refresh_sessions()
            return
        if channel.startswith(uris.SESSION_PREFIX):
            sid = uris.session_id(channel)
            session = await self._known(sid) if sid else None
            for record in self._mapper.map(channel, action):
                if session is not None and isinstance(record, mp.TitleChanged):
                    await self._registry.update_session_fields(session.session_key, title=record.title[:80])
            return
        owner = uris.chat_session(channel)
        sid = uris.session_id(owner) if owner else None
        session = await self._known(sid) if sid else None
        if session is None:
            return
        for record in self._mapper.map(channel, action):
            await self._apply(session, record)

    async def _emit[E: Event](self, kind: type[E], session: SessionRecord, **fields: object) -> None:
        if self._publisher is None:
            return
        event = kind(
            harness=session.harness, session_key=session.session_key, workspace=session.workspace, **fields
        )
        await self._publisher.publish(event)

    async def _still_open(self, request_id: str) -> bool:
        pending = await self._registry.get_pending(request_id)
        return pending is not None and pending.status == "open"

    async def _end_turn(self, session: SessionRecord, hooked: bool) -> None:
        await self._registry.set_session_extra(session.session_key, TURN_EXTRA, None)
        self._turn_started.pop(session.session_key, None)
        if not hooked and session.status is not SessionStatus.PARKED:
            await self.report_status(session.session_key, SessionStatus.IDLE, "adapter report")

    async def _apply(self, session: SessionRecord, record: mp.Record) -> None:
        hooked = session.extra.get(HOOKED) is True
        match record:
            case mp.TurnStarted(turn_id=turn_id):
                await self._registry.set_session_extra(session.session_key, TURN_EXTRA, turn_id)
                self._turn_started.setdefault(session.session_key, time.monotonic())
                if not hooked and session.status is not SessionStatus.RUNNING:
                    await self.report_status(session.session_key, SessionStatus.RUNNING, "adapter report")
            case mp.Delta(text=text):
                await self._emit(TextDelta, session, text=text)
            case mp.ToolStart(call_id=call_id, name=name, detail=detail):
                if not hooked:
                    await self._emit(ToolStarted, session, tool_id=call_id, name=name, detail=detail)
            case mp.ToolAsk():
                await self._ask(session, record)
            case mp.ToolConfirmed(call_id=call_id, approved=approved):
                if self._asks.pop(call_id, None) is not None and await self._still_open(call_id):
                    decision: Decision = "allow" if approved else "deny"
                    await self._emit(
                        ApprovalResolved, session, request_id=call_id, decision=decision, by="desk"
                    )
            case mp.ToolEnd(call_id=call_id, name=name, ok=ok, summary=summary):
                if not hooked:
                    await self._emit(
                        ToolFinished, session, tool_id=call_id, name=name, ok=ok, summary=summary
                    )
            case mp.InputAsk(request_id=request_id, question=question, options=options):
                self._inputs[request_id] = record
                await self._emit(
                    QuestionAsked, session, request_id=request_id, question=question, options=options
                )
            case mp.InputDone(request_id=request_id, answer=answer):
                if self._inputs.pop(request_id, None) is not None and await self._still_open(request_id):
                    await self._emit(QuestionAnswered, session, request_id=request_id, answer=answer)
            case mp.TurnDone(text=text):
                await self._end_turn(session, hooked)
                if not hooked:
                    await self._emit(TurnCompleted, session, text=text)
            case mp.TurnCancelled():
                await self._end_turn(session, hooked)
                if not hooked:
                    await self._emit(TurnCompleted, session, text=None)
            case mp.Failed(message=message):
                await self._end_turn(session, hooked)
                if not hooked:
                    await self._emit(ErrorEvent, session, message=message)
            case mp.ChatAdded() | mp.TitleChanged():
                return

    async def _ask(self, session: SessionRecord, ask: mp.ToolAsk) -> None:
        self._asks[ask.call_id] = ask
        prior = await self._registry.kv_get(APPROVED_CALL_KEY.format(call_id=ask.call_id))
        if prior in ("allow", "deny"):
            decision: Decision = "allow" if prior == "allow" else "deny"
            await self.answer_approval(session, ask.call_id, decision)
            log.info(f"agent host tool call {ask.call_id} answered from the earlier phone decision")
            return
        await self._emit(ApprovalRequested, session, request_id=ask.call_id, tool=ask.tool, detail=ask.detail)

    # the Adapter contract

    async def health(self) -> tuple[bool, str]:
        if self.connected:
            return True, f"VS Code Agent Host connected (AHP {self._version})"
        try:
            endpoint = await self._discover()
        except AhpUnavailableError as exc:
            return False, str(exc)
        return False, f"VS Code Agent Host found (AHP {endpoint.protocol_version}) but not connected yet"

    async def list_models(self) -> list[ModelInfo]:
        await self._live()
        agents = self._root.agents if self._root else []
        agent = next((a for a in agents if a.provider == PROVIDER), None)
        if agent is None:
            raise AdapterUnavailableError("VS Code's Agent Host offers no Copilot agent")
        return [
            ModelInfo(id=model.id, label=model.name, default=model.id == DEFAULT_MODEL)
            for model in agent.models
        ]

    async def start_session(
        self, request: StartRequest, attachments: Sequence[Attachment] = ()
    ) -> SessionRecord:
        conn = await self._live()
        sid = str(uuid.uuid4())
        session = uris.session_uri(sid)
        chat = uris.chat_uri(str(uuid.uuid4()), session)
        title = request.prompt.strip().splitlines()[0][:80] if request.prompt.strip() else "phone session"
        try:
            await conn.request(
                "createSession",
                m.CreateSessionParams(
                    channel=session,
                    provider=PROVIDER,
                    working_directories=[uris.file_uri(request.workspace)],
                    config={"isolation": "worktree" if request.isolation else "folder"},
                ),
            )
            await self._subscribe(conn, session)
            record = await self._registry.create_session(
                SessionRecord(
                    session_key=f"{self.harness.value}:{sid}",
                    harness=self.harness,
                    harness_id=sid,
                    workspace=request.workspace,
                    model=request.model,
                    title=title,
                    status=SessionStatus.RUNNING,
                    origin=Origin.PHONE,
                    extra={SESSION_EXTRA: session, CHAT_EXTRA: chat, PROVIDER_EXTRA: PROVIDER},
                )
            )
            await make_claim(self._registry, request.workspace, record.session_key)
            await conn.request("createChat", m.CreateChatParams(channel=session, chat=chat))
            await self._subscribe(conn, chat)
        except AhpRpcError as exc:
            raise RelayError(f"VS Code refused the new session: {exc}") from exc
        await self._start_turn(conn, record, chat, request.prompt, request.model, attachments)
        return await self._registry.get_session(record.session_key) or record

    async def _chat_of(self, session: SessionRecord) -> str:
        chat = session.extra.get(CHAT_EXTRA)
        if isinstance(chat, str):
            return chat
        await self.refresh_sessions()
        fresh = await self._registry.get_session(session.session_key)
        chat = fresh.extra.get(CHAT_EXTRA) if fresh else None
        if isinstance(chat, str):
            return chat
        raise UnsupportedError(self.harness, Capability.WAKE_IDLE)

    async def _message(
        self, text: str, model: str | None, attachments: Sequence[Attachment] = ()
    ) -> m.Message:
        return m.Message(
            text=text,
            origin=m.MessageOrigin(kind="user"),
            model=m.ModelSelection(id=model or DEFAULT_MODEL),
            attachments=[await _message_attachment(a) for a in attachments] or None,
        )

    async def _start_turn(
        self,
        conn: Connection,
        session: SessionRecord,
        chat: str,
        text: str,
        model: str | None,
        attachments: Sequence[Attachment] = (),
    ) -> None:
        turn_id = str(uuid.uuid4())
        action = m.ChatTurnStartedAction(
            turn_id=turn_id, started_at=_now_iso(), message=await self._message(text, model, attachments)
        )
        await self._dispatch(conn, chat, action)
        await self._registry.set_session_extra(session.session_key, TURN_EXTRA, turn_id)
        self._turn_started[session.session_key] = time.monotonic()

    async def send_prompt(
        self, session: SessionRecord, text: str, attachments: Sequence[Attachment] = ()
    ) -> None:
        conn = await self._live()
        chat = await self._chat_of(session)
        fresh = await self._registry.get_session(session.session_key) or session
        await self._ensure_subscribed(conn, fresh)
        if isinstance(fresh.extra.get(TURN_EXTRA), str):
            steer = m.ChatPendingMessageSetAction(
                kind="steering",
                id=str(uuid.uuid4()),
                message=await self._message(text, fresh.model, attachments),
            )
            await self._dispatch(conn, chat, steer)
            return
        await self._start_turn(conn, fresh, chat, text, fresh.model, attachments)

    async def wake(self, session: SessionRecord, text: str) -> None:
        await self.send_prompt(session, text)

    async def set_model(self, session: SessionRecord, model: str) -> None:
        known = {info.id for info in await self.list_models()}
        if model not in known:
            raise RelayError(f"VS Code Copilot has no model `{model}`. Try `/models`.")
        log.info(f"{session.session_key} will use {model} from the next turn")

    async def cancel(self, session: SessionRecord) -> None:
        conn = await self._live()
        fresh = await self._registry.get_session(session.session_key) or session
        turn_id = fresh.extra.get(TURN_EXTRA)
        if not isinstance(turn_id, str):
            log.info(f"{session.session_key} has no running turn to cancel")
            return
        started = self._turn_started.get(session.session_key)
        elapsed = int((time.monotonic() - started) * 1000) if started is not None else 0
        await self._dispatch(
            conn, await self._chat_of(fresh), m.ChatTurnCancelledAction(turn_id=turn_id, duration=elapsed)
        )

    def _option(self, ask: mp.ToolAsk | None, decision: Decision) -> str | None:
        if ask is None or not ask.options:
            return None
        wanted = "deny" if decision == "deny" else "approve"
        matching = [o for o in ask.options if o.kind == wanted]
        if decision == "allow_session":
            scoped = [o for o in matching if "session" in f"{o.id} {o.label}".lower()]
            matching = scoped or matching
        return matching[0].id if matching else None

    async def answer_approval(
        self, session: SessionRecord, request_id: str, decision: Decision, reason: str | None = None
    ) -> None:
        conn = await self._live()
        ask = self._asks.pop(request_id, None)
        fresh = await self._registry.get_session(session.session_key) or session
        turn = ask.turn_id if ask else fresh.extra.get(TURN_EXTRA)
        if not isinstance(turn, str):
            raise RelayError("That tool call is no longer waiting in VS Code.")
        chat = ask.chat if ask else await self._chat_of(fresh)
        action: m.AhpModel
        if decision == "deny":
            action = m.ChatToolCallDeniedAction(
                turn_id=turn,
                tool_call_id=request_id,
                approved=False,
                reason="denied",
                reason_message=reason,
                selected_option_id=self._option(ask, decision),
            )
        else:
            action = m.ChatToolCallApprovedAction(
                turn_id=turn,
                tool_call_id=request_id,
                approved=True,
                confirmed="user-action",
                selected_option_id=self._option(ask, decision),
            )
        await self._dispatch(conn, chat, action)

    async def answer_question(self, session: SessionRecord, request_id: str, answer: str | None) -> None:
        conn = await self._live()
        ask = self._inputs.pop(request_id, None)
        response, answers = mp.build_answers(ask.questions if ask else (), answer)
        chat = ask.chat if ask else await self._chat_of(session)
        completed = m.ChatInputCompletedAction(request_id=request_id, response=response, answers=answers)
        await self._dispatch(conn, chat, completed)

    async def aclose(self) -> None:
        await self._drop()
