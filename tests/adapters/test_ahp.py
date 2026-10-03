"""AC-15: the AHP adapter against a fake Agent Host that replays the recorded S1 trace."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay.adapters.ahp import uris
from agent_relay.adapters.ahp.adapter import CHAT_EXTRA, TURN_EXTRA, AhpAdapter
from agent_relay.adapters.ahp.endpoints import Endpoint
from agent_relay.adapters.ahp.rpc import AhpConnectionError, AhpRpcError, Connection
from agent_relay.adapters.base import UnsupportedError
from agent_relay.core import away
from agent_relay.core.hooks import APPROVED_CALL_KEY, HOOKED
from agent_relay.core.registry import Registry
from agent_relay.core.service import RelayError
from agent_relay.daemon import Core
from agent_relay.models.config import RelayConfig, Workspace, WorkspacesConfig
from agent_relay.models.events import (
    ApprovalRequested,
    ApprovalResolved,
    ErrorEvent,
    Event,
    Harness,
    Origin,
    QuestionAsked,
    SessionStarted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import save_workspaces
from tests.fakes.ahp import FakeAgentHost

WORKSPACE = "/w/proj"


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def of[T](self, kind: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, kind)]


class Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)


async def until(check: Callable[[], object], timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.005)):
        result = check()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met")


async def fast_sleep(_: float) -> None:
    await asyncio.sleep(0.005)


@dataclass
class Rig:
    core: Core
    host: FakeAgentHost
    adapter: AhpAdapter
    rec: Recorder
    registry: Registry

    async def start(self, prompt: str = "Reply with exactly: OK", model: str | None = None) -> SessionRecord:
        return await self.core.service.start("copilot", "proj", prompt, model)

    async def session(self, key: str) -> SessionRecord:
        found = await self.registry.get_session(key)
        assert found is not None
        return found

    def chat_of(self, record: SessionRecord) -> str:
        chat = record.extra.get(CHAT_EXTRA)
        assert isinstance(chat, str)
        return chat


@pytest.fixture
async def rig(registry: Registry, relay_home: Path) -> AsyncIterator[Rig]:
    save_workspaces(WorkspacesConfig(workspaces=[Workspace(name="proj", path=WORKSPACE)]))
    host = await FakeAgentHost(str(relay_home / "ah.sock")).start()

    async def found() -> Endpoint:
        return host.endpoint

    core = Core(RelayConfig(), registry)
    rec = Recorder()
    core.router.subscribe(rec)
    adapter = AhpAdapter(registry, discover_endpoint=found, sleep=fast_sleep, backoff=(0.01, 0.02))
    core.router.add_adapter(adapter)
    await until(lambda: adapter.connected)
    yield Rig(core, host, adapter, rec, registry)
    await core.router.stop()
    await host.stop()


async def test_phone_session_replays_the_recorded_turn(rig: Rig) -> None:
    record = await rig.start()
    await until(lambda: rig.rec.of(TurnCompleted))
    created = dict(rig.host.requests)["createSession"]
    assert created["provider"] == "copilotcli"
    assert created["config"] == {"isolation": "folder"}
    assert created["workingDirectories"] == [Path(WORKSPACE).resolve().as_uri()]
    chat = rig.chat_of(record)
    assert uris.chat_session(chat) == uris.session_uri(record.harness_id)
    assert rig.host.methods()[:5] == [
        "initialize",
        "listSessions",
        "createSession",
        "subscribe",
        "createChat",
    ]
    (turn,) = rig.host.actions("chat/turnStarted")
    message = turn["message"]
    assert isinstance(message, dict)
    assert message == {"text": "Reply with exactly: OK", "origin": {"kind": "user"}, "model": {"id": "auto"}}
    deltas = [e.text for e in rig.rec.of(TextDelta)]
    assert deltas[-1] == "OK"
    (done,) = rig.rec.of(TurnCompleted)
    assert done.text is not None and done.text.endswith("OK")
    assert (await rig.session(record.session_key)).status is SessionStatus.IDLE
    assert (await rig.session(record.session_key)).extra.get(TURN_EXTRA) is None
    started = rig.rec.of(SessionStarted)
    assert [s.origin for s in started] == [Origin.PHONE]


async def test_wrong_token_never_connects(registry: Registry, relay_home: Path) -> None:
    host = await FakeAgentHost(str(relay_home / "ah.sock")).start()

    async def stale() -> Endpoint:
        return Endpoint(host.socket_path, "0.9.0", "stale-token")

    adapter = AhpAdapter(registry, discover_endpoint=stale, sleep=fast_sleep, backoff=(0.01, 0.02))
    task = asyncio.create_task(adapter.run(Sink()))
    await until(lambda: host.rejected >= 2)
    healthy, detail = await adapter.health()
    assert not healthy and "not connected" in detail
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await host.stop()


async def test_models_come_from_the_root_snapshot(rig: Rig) -> None:
    models = await rig.core.service.models("copilot")
    assert models[0].id == "auto" and models[0].default
    assert "claude-sonnet-5" in {m.id for m in models}
    assert all(not m.id.startswith("@provider") for m in models)


async def test_set_model_validates_and_applies_to_the_next_turn(rig: Rig) -> None:
    record = await rig.start()
    await until(lambda: rig.rec.of(TurnCompleted))
    with pytest.raises(RelayError, match="no model"):
        await rig.core.service.set_model(record.session_key, "gpt-1")
    await rig.core.service.set_model(record.session_key, "claude-sonnet-5")
    assert await rig.core.service.prompt(record.session_key, "again") == "sent"
    await until(lambda: len(rig.host.actions("chat/turnStarted")) == 2)
    second = rig.host.actions("chat/turnStarted")[1]["message"]
    assert isinstance(second, dict) and second["model"] == {"id": "claude-sonnet-5"}


async def quiet_start(rig: Rig) -> tuple[SessionRecord, str, str]:
    """A phone session whose turn stays open, so the test drives the server side."""
    rig.host.auto_reply = False
    record = await rig.start()
    await until(lambda: rig.host.actions("chat/turnStarted"))
    turn = rig.host.actions("chat/turnStarted")[0]["turnId"]
    assert isinstance(turn, str)
    return record, rig.chat_of(record), turn


async def ask_tool(rig: Rig, chat: str, turn: str, call: str, options: list[JsonValue] | None = None) -> None:
    await rig.host.push(
        chat,
        {
            "type": "chat/toolCallStart",
            "turnId": turn,
            "toolCallId": call,
            "toolName": "bash",
            "displayName": "Shell",
        },
    )
    ready: dict[str, JsonValue] = {
        "type": "chat/toolCallReady",
        "turnId": turn,
        "toolCallId": call,
        "invocationMessage": {"markdown": "Run `rm -rf build`"},
        "toolInput": "rm -rf build",
    }
    if options is not None:
        ready["options"] = options
    await rig.host.push(chat, ready)


async def test_steering_while_a_turn_runs(rig: Rig) -> None:
    record, chat, _ = await quiet_start(rig)
    assert await rig.core.service.prompt(record.session_key, "also update the docs") == "sent"
    await until(lambda: rig.host.actions("chat/pendingMessageSet"))
    (steer,) = rig.host.actions("chat/pendingMessageSet")
    assert steer["kind"] == "steering"
    message = steer["message"]
    assert isinstance(message, dict) and message["text"] == "also update the docs"
    assert rig.host.dispatched[-1]["channel"] == chat


async def test_tool_approval_round_trip(rig: Rig) -> None:
    _, chat, turn = await quiet_start(rig)
    await ask_tool(rig, chat, turn, "call-1")
    await until(lambda: rig.rec.of(ApprovalRequested))
    (asked,) = rig.rec.of(ApprovalRequested)
    assert (asked.request_id, asked.tool, asked.detail) == ("call-1", "Shell", "Run `rm -rf build`")
    assert [t.name for t in rig.rec.of(ToolStarted)] == ["Shell"]
    assert await rig.core.service.answer_approval("call-1", "allow")
    await until(lambda: rig.host.actions("chat/toolCallConfirmed"))
    (confirmed,) = rig.host.actions("chat/toolCallConfirmed")
    assert confirmed == {
        "type": "chat/toolCallConfirmed",
        "turnId": turn,
        "toolCallId": "call-1",
        "approved": True,
        "confirmed": "user-action",
    }


async def test_deny_and_session_scoped_options(rig: Rig) -> None:
    _, chat, turn = await quiet_start(rig)
    options: list[JsonValue] = [
        {"id": "once", "label": "Allow", "kind": "approve"},
        {"id": "session", "label": "Allow in this Session", "kind": "approve"},
        {"id": "no", "label": "Deny", "kind": "deny"},
    ]
    await ask_tool(rig, chat, turn, "call-2", options)
    await ask_tool(rig, chat, turn, "call-3", options)
    await until(lambda: len(rig.rec.of(ApprovalRequested)) == 2)
    assert await rig.core.service.answer_approval("call-2", "deny")
    assert await rig.core.service.answer_approval("call-3", "allow_session")
    await until(lambda: len(rig.host.actions("chat/toolCallConfirmed")) == 2)
    denied, scoped = rig.host.actions("chat/toolCallConfirmed")
    assert (denied["approved"], denied["reason"], denied["selectedOptionId"]) == (False, "denied", "no")
    assert (scoped["approved"], scoped["selectedOptionId"]) == (True, "session")


async def test_approval_answered_at_the_desk_closes_the_card(rig: Rig) -> None:
    _, chat, turn = await quiet_start(rig)
    await ask_tool(rig, chat, turn, "call-4")
    await until(lambda: rig.core.registry.get_pending("call-4"))
    await rig.host.push(
        chat,
        {
            "type": "chat/toolCallConfirmed",
            "turnId": turn,
            "toolCallId": "call-4",
            "approved": True,
            "confirmed": "user-action",
        },
    )
    await until(lambda: rig.rec.of(ApprovalResolved))
    (resolved,) = rig.rec.of(ApprovalResolved)
    assert (resolved.decision, resolved.by) == ("allow", "desk")
    pending = await rig.core.registry.get_pending("call-4")
    assert pending is not None and pending.status != "open"
    assert rig.host.actions("chat/toolCallConfirmed") == []


async def test_earlier_phone_decision_is_reused_without_a_second_card(rig: Rig) -> None:
    _, chat, turn = await quiet_start(rig)
    await rig.core.registry.kv_set(APPROVED_CALL_KEY.format(call_id="call-5"), "deny")
    await ask_tool(rig, chat, turn, "call-5")
    await until(lambda: rig.host.actions("chat/toolCallConfirmed"))
    assert rig.host.actions("chat/toolCallConfirmed")[0]["approved"] is False
    assert rig.rec.of(ApprovalRequested) == []


async def test_tool_completion_and_errors(rig: Rig) -> None:
    record, chat, turn = await quiet_start(rig)
    await ask_tool(rig, chat, turn, "call-6")
    await rig.host.push(
        chat,
        {
            "type": "chat/toolCallComplete",
            "turnId": turn,
            "toolCallId": "call-6",
            "result": {"success": False, "pastTenseMessage": "Ran", "error": {"message": "exit 1"}},
        },
    )
    await until(lambda: rig.rec.of(ToolFinished))
    (finished,) = rig.rec.of(ToolFinished)
    assert (finished.name, finished.ok, finished.summary) == ("Shell", False, "exit 1")
    await rig.host.push(
        chat,
        {
            "type": "chat/error",
            "turnId": turn,
            "duration": 5,
            "part": {"kind": "error", "error": {"errorType": "quota", "message": "rate limited"}},
        },
    )
    await until(lambda: rig.rec.of(ErrorEvent))
    assert rig.rec.of(ErrorEvent)[0].message == "rate limited"
    assert (await rig.session(record.session_key)).status is SessionStatus.IDLE


async def test_input_request_round_trip(rig: Rig) -> None:
    _, chat, turn = await quiet_start(rig)
    request: dict[str, JsonValue] = {
        "id": "req-1",
        "questions": [
            {
                "id": "q1",
                "message": "Which branch?",
                "kind": "single-select",
                "options": [{"id": "main", "label": "main"}, {"id": "dev", "label": "develop"}],
            }
        ],
    }
    await rig.host.push(chat, {"type": "chat/inputRequested", "request": request})
    await until(lambda: rig.rec.of(QuestionAsked))
    (asked,) = rig.rec.of(QuestionAsked)
    assert (asked.request_id, asked.question, asked.options) == (
        "req-1",
        "Which branch?",
        ("main", "develop"),
    )
    assert await rig.core.service.answer_question("req-1", "2")
    await until(lambda: rig.host.actions("chat/inputCompleted"))
    (completed,) = rig.host.actions("chat/inputCompleted")
    assert completed == {
        "type": "chat/inputCompleted",
        "requestId": "req-1",
        "response": "accept",
        "answers": {"q1": {"state": "submitted", "value": {"kind": "selected", "value": "dev"}}},
    }
    assert turn


async def test_cancel_sends_turn_cancelled(rig: Rig) -> None:
    record, chat, turn = await quiet_start(rig)
    await rig.core.service.stop(record.session_key)
    await until(lambda: rig.host.actions("chat/turnCancelled"))
    (cancelled,) = rig.host.actions("chat/turnCancelled")
    assert cancelled["turnId"] == turn and isinstance(cancelled["duration"], int)
    await rig.host.push(chat, {"type": "chat/turnCancelled", "turnId": turn, "duration": 3})
    await until(lambda: rig.rec.of(TurnCompleted))
    assert (await rig.session(record.session_key)).extra.get(TURN_EXTRA) is None


async def hook_session(rig: Rig, sid: str) -> SessionRecord:
    return await rig.registry.upsert_session(
        SessionRecord(
            session_key=f"copilot_cli:{sid}",
            harness=Harness.COPILOT_CLI,
            harness_id=sid,
            workspace=WORKSPACE,
            origin=Origin.DESK,
            status=SessionStatus.PARKED,
            extra={HOOKED: True},
        )
    )


async def test_hooked_desk_session_is_refined_and_streams_text_only(rig: Rig) -> None:
    sid = "11111111-2222-3333-4444-555555555555"
    record = await hook_session(rig, sid)
    session = rig.host.add_session(sid, WORKSPACE)
    await rig.host.push(uris.ROOT, {"type": "root/activeSessionsChanged", "activeSessions": 1})
    chat = uris.chat_uri(uris.DEFAULT_CHAT, session)
    await until(lambda: ("subscribe", {"channel": chat}) in rig.host.requests)
    refined = await rig.session(record.session_key)
    assert refined.harness is Harness.VSCODE_COPILOT and refined.extra[CHAT_EXTRA] == chat
    await rig.host.replay_turn(session, chat, "turn-9")
    await until(lambda: [e for e in rig.rec.of(TextDelta) if e.text == "OK"])
    assert rig.rec.of(TurnCompleted) == [] and rig.rec.of(ToolStarted) == []
    assert (await rig.session(record.session_key)).status is SessionStatus.PARKED
    assert rig.rec.of(SessionStarted) == []


async def test_wake_reaches_a_parked_hook_session_through_its_chat(rig: Rig) -> None:
    sid = "66666666-2222-3333-4444-555555555555"
    await hook_session(rig, sid)
    rig.host.add_session(sid, WORKSPACE, status=1)
    await rig.adapter.refresh_sessions()
    refined = await rig.session(f"copilot_cli:{sid}")
    await rig.adapter.wake(refined, "carry on")
    await until(lambda: rig.host.actions("chat/turnStarted"))
    assert rig.host.dispatched[-1]["channel"] == uris.chat_uri(uris.DEFAULT_CHAT, uris.session_uri(sid))


async def test_wake_without_a_host_session_is_unsupported(rig: Rig) -> None:
    record = await hook_session(rig, "77777777-2222-3333-4444-555555555555")
    with pytest.raises(UnsupportedError):
        await rig.adapter.wake(record, "hello")


async def test_active_desk_sessions_are_adopted_only_while_away(rig: Rig) -> None:
    rig.host.add_session("88888888-2222-3333-4444-555555555555", WORKSPACE, title="refactor")
    await rig.adapter.refresh_sessions()
    assert (
        await rig.registry.find_session(Harness.VSCODE_COPILOT, "88888888-2222-3333-4444-555555555555")
        is None
    )
    await away.set_away(rig.registry, True, None)
    rig.host.add_session("99999999-2222-3333-4444-555555555555", WORKSPACE, status=1, title="idle one")
    await rig.adapter.refresh_sessions()
    adopted = await rig.registry.find_session(Harness.VSCODE_COPILOT, "88888888-2222-3333-4444-555555555555")
    assert adopted is not None and adopted.origin is Origin.DESK and adopted.title == "refactor"
    assert (
        await rig.registry.find_session(Harness.VSCODE_COPILOT, "99999999-2222-3333-4444-555555555555")
        is None
    )
    assert [s.title for s in rig.rec.of(SessionStarted)] == ["refactor"]


async def test_reconnects_and_resubscribes_after_the_socket_drops(rig: Rig) -> None:
    record = await rig.start()
    await until(lambda: rig.rec.of(TurnCompleted))
    chat = rig.chat_of(record)
    await rig.host.drop_clients()
    await until(lambda: rig.host.methods().count("initialize") == 2)
    await until(lambda: rig.adapter.connected)
    await until(lambda: rig.host.requests.count(("subscribe", {"channel": chat})) == 2)
    assert rig.rec.of(ErrorEvent) == []


async def test_rpc_errors_and_closed_sockets(relay_home: Path) -> None:
    host = await FakeAgentHost(str(relay_home / "rpc.sock")).start()
    conn = await Connection.open(host.endpoint)
    with pytest.raises(AhpRpcError, match="bogus failed"):
        await conn.request("bogus", {})
    await host.drop_clients()
    await until(lambda: conn.closed)
    with pytest.raises(AhpConnectionError):
        await conn.request("listSessions", {"channel": uris.ROOT})
    await conn.close()
    await host.stop()
    with pytest.raises(AhpConnectionError):
        await Connection.open(host.endpoint)
