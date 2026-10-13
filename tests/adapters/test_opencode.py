"""AC-19: the OpenCode adapter against a fake server serving the recorded `/doc` and SSE."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from agent_relay.adapters.base import AdapterUnavailableError, Decision
from agent_relay.adapters.opencode.adapter import OpenCodeAdapter
from agent_relay.adapters.opencode.client import OpenCodeClient, ServerEvent, parse_sse
from agent_relay.adapters.opencode.mapping import Delta, EventMapper, Idle
from agent_relay.core import away
from agent_relay.core.registry import Registry
from agent_relay.core.service import RelayError
from agent_relay.daemon import Core
from agent_relay.models.config import OpenCodeConfig, RelayConfig, Workspace, WorkspacesConfig
from agent_relay.models.events import (
    ApprovalRequested,
    ApprovalResolved,
    ErrorEvent,
    Event,
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
from tests.fakes.opencode import OK_SSE, FakeOpenCode, FakeOpenCodeFleet

PRIMARY = "http://127.0.0.1:4096"
OTHER = "http://127.0.0.1:5123"


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def of[T](self, kind: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, kind)]


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
    fake: FakeOpenCode
    other: FakeOpenCode
    adapter: OpenCodeAdapter
    rec: Recorder

    async def connected(self, count: int = 1) -> None:
        await until(lambda: self.fake.subscribers >= count)

    async def start(self, prompt: str = "Reply with exactly: OK", model: str | None = None) -> SessionRecord:
        return await self.core.service.start("opencode", "proj", prompt, model)


def _rig_factory(password: str | None, legacy: bool) -> Callable[[Registry], AsyncIterator[Rig]]:
    async def build(registry: Registry) -> AsyncIterator[Rig]:
        save_workspaces(WorkspacesConfig(workspaces=[Workspace(name="proj", path="/w/proj")]))
        fake = FakeOpenCode(PRIMARY, password=password, legacy=legacy)
        other = FakeOpenCode(OTHER)
        fleet = FakeOpenCodeFleet(fake, other)
        async with fleet.client() as http:
            core = Core(RelayConfig(), registry)
            rec = Recorder()
            core.router.subscribe(rec)
            adapter = OpenCodeAdapter(
                OpenCodeConfig(url=PRIMARY), registry, password, http, sleep=fast_sleep, backoff=(0.01, 0.02)
            )
            core.router.add_adapter(adapter)
            rig = Rig(core, fake, other, adapter, rec)
            await rig.connected()
            yield rig
            fake.close_streams()
            other.close_streams()
            await core.router.stop()

    return build


@pytest.fixture
async def rig(registry: Registry) -> AsyncIterator[Rig]:
    async for built in _rig_factory(None, legacy=False)(registry):
        yield built


@pytest.fixture
async def legacy_rig(registry: Registry) -> AsyncIterator[Rig]:
    async for built in _rig_factory("s3cret-test", legacy=True)(registry):
        yield built


def test_recorded_session_maps_to_one_delta_and_final_text() -> None:
    mapper = EventMapper()
    records = []
    for block in OK_SSE.read_text().split("\n\n"):
        event = parse_sse(block.splitlines())
        if event is not None:
            records.extend(mapper.map(event))
    assert [r.text for r in records if isinstance(r, Delta)] == ["OK"]
    assert [r.text for r in records if isinstance(r, Idle)] == ["OK"]


def test_mapper_skips_reasoning_and_user_text_and_fills_unstreamed_text() -> None:
    mapper = EventMapper()

    def ev(kind: str, **props: JsonValue) -> ServerEvent:
        return ServerEvent(kind, dict(props), "/w")

    mapper.map(ev("message.updated", sessionID="s", info={"id": "u", "role": "user"}))
    mapper.map(ev("message.updated", sessionID="s", info={"id": "a", "role": "assistant"}))
    user: dict[str, JsonValue] = {"id": "p0", "messageID": "u", "type": "text", "text": "secret prompt"}
    assert mapper.map(ev("message.part.updated", sessionID="s", part=user)) == []
    think: dict[str, JsonValue] = {"id": "p1", "messageID": "a", "type": "reasoning", "text": "hmm"}
    mapper.map(ev("message.part.updated", sessionID="s", part=think))
    delta = {"sessionID": "s", "messageID": "a", "partID": "p1", "field": "text", "delta": "thinking"}
    assert mapper.map(ev("message.part.delta", **delta)) == []
    whole: dict[str, JsonValue] = {"id": "p2", "messageID": "a", "type": "text", "text": "Hello there"}
    assert mapper.map(ev("message.part.updated", sessionID="s", part=whole)) == [Delta("s", "Hello there")]
    assert mapper.map(ev("message.part.updated", sessionID="s", part=whole)) == []
    assert mapper.map(ev("session.idle", sessionID="s")) == [Idle("s", "Hello there")]
    assert mapper.map(ev("session.idle", sessionID="s")) == [Idle("s", None)]


async def test_phone_session_streams_recorded_turn(rig: Rig) -> None:
    session = await rig.start()
    sid = session.harness_id
    assert rig.fake.sessions[sid] == "/w/proj"
    prompt_sid, body, directory = rig.fake.prompts[0]
    assert prompt_sid == sid and directory == "/w/proj"
    assert body == {"parts": [{"type": "text", "text": "Reply with exactly: OK"}]}
    rig.fake.replay(session_id=sid)
    await until(lambda: rig.rec.of(TurnCompleted))
    assert [e.text for e in rig.rec.of(TextDelta)] == ["OK"]
    assert rig.rec.of(TurnCompleted)[0].text == "OK"
    stored = await rig.core.registry.get_session(session.session_key)
    assert stored is not None and stored.status is SessionStatus.IDLE and stored.origin is Origin.PHONE
    assert [e.origin for e in rig.rec.of(SessionStarted)] == [Origin.PHONE]


async def test_model_is_sent_as_provider_and_model(rig: Rig) -> None:
    await rig.start("hi", model="github-copilot/gpt-5-mini")
    assert rig.fake.prompts[0][1]["model"] == {"providerID": "github-copilot", "modelID": "gpt-5-mini"}


async def test_desk_sessions_stay_off_the_phone_until_away(rig: Rig) -> None:
    rig.fake.ok_turn("ses_desk", "at the desk")
    await asyncio.sleep(0.05)
    assert rig.rec.events == []
    assert await rig.core.registry.list_sessions() == []
    await away.set_away(rig.core.registry, True)
    rig.fake.ok_turn("ses_desk2", "while away")
    await until(lambda: rig.rec.of(TurnCompleted))
    started = rig.rec.of(SessionStarted)
    assert [(s.session_key, s.origin) for s in started] == [("opencode:ses_desk2", Origin.DESK)]
    assert rig.rec.of(TurnCompleted)[0].text == "while away"


def _ask(sid: str, request_id: str = "per_1", call_id: str | None = None) -> dict[str, object]:
    props: dict[str, object] = {
        "id": request_id,
        "sessionID": sid,
        "permission": "bash",
        "patterns": ["rm -rf build"],
    }
    if call_id:
        props["tool"] = {"messageID": "msg_1", "callID": call_id}
    return props


@pytest.mark.parametrize(
    ("decision", "reply"),
    [("allow", "once"), ("allow_session", "always"), ("allow_workspace", "once"), ("deny", "reject")],
)
async def test_permission_round_trip_current_endpoint(rig: Rig, decision: Decision, reply: str) -> None:
    session = await rig.start()
    rig.fake.emit("permission.asked", _ask(session.harness_id))
    await until(lambda: rig.rec.of(ApprovalRequested))
    asked = rig.rec.of(ApprovalRequested)[0]
    assert (asked.request_id, asked.tool, asked.detail) == ("per_1", "bash", "bash: rm -rf build")
    await until(lambda: rig.core.registry.get_pending("per_1"))
    pending = await rig.core.registry.get_pending("per_1")
    assert pending is not None and pending.payload["source"] == "adapter"
    assert await rig.core.service.answer_approval("per_1", decision)
    assert rig.fake.replies == [("permission:per_1", {"reply": reply})]


async def test_a_deny_reason_travels_as_the_reply_message_on_the_current_endpoint(rig: Rig) -> None:
    session = await rig.start()
    rig.fake.emit("permission.asked", _ask(session.harness_id))
    await until(lambda: rig.core.registry.get_pending("per_1"))
    assert await rig.core.service.answer_approval("per_1", "deny", reason="use the staging db")
    assert rig.fake.replies == [("permission:per_1", {"reply": "reject", "message": "use the staging db"})]


async def test_the_legacy_endpoint_has_no_message_field(legacy_rig: Rig) -> None:
    session = await legacy_rig.start()
    legacy_rig.fake.emit("permission.asked", _ask(session.harness_id))
    await until(lambda: legacy_rig.core.registry.get_pending("per_1"))
    assert await legacy_rig.core.service.answer_approval("per_1", "deny", reason="nope")
    assert legacy_rig.fake.replies == [(f"legacy:{session.harness_id}:per_1", {"response": "reject"})]


async def test_permission_legacy_endpoint_and_basic_auth(legacy_rig: Rig) -> None:
    session = await legacy_rig.start()
    legacy_rig.fake.emit("permission.asked", _ask(session.harness_id))
    await until(lambda: legacy_rig.rec.of(ApprovalRequested))
    await until(lambda: legacy_rig.core.registry.get_pending("per_1"))
    assert await legacy_rig.core.service.answer_approval("per_1", "deny")
    assert legacy_rig.fake.replies == [(f"legacy:{session.harness_id}:per_1", {"response": "reject"})]
    assert legacy_rig.fake.unauthorized == 0


async def test_permission_already_decided_through_the_plugin_is_answered_quietly(rig: Rig) -> None:
    session = await rig.start()
    await rig.core.registry.kv_set("approved_call:call_9", "allow")
    rig.fake.emit("permission.asked", _ask(session.harness_id, call_id="call_9"))
    await until(lambda: rig.fake.replies)
    assert rig.fake.replies == [("permission:per_1", {"reply": "once"})]
    assert not rig.rec.of(ApprovalRequested)


async def test_permission_answered_at_the_desk_closes_the_card_without_a_second_reply(rig: Rig) -> None:
    session = await rig.start()
    sid = session.harness_id
    rig.fake.emit("permission.asked", _ask(sid))
    await until(lambda: rig.core.registry.get_pending("per_1"))
    rig.fake.emit("permission.replied", {"sessionID": sid, "requestID": "per_1", "reply": "always"})
    await until(lambda: rig.rec.of(ApprovalResolved))
    assert [(e.decision, e.by) for e in rig.rec.of(ApprovalResolved)] == [("allow_session", "desk")]
    pending = await rig.core.registry.get_pending("per_1")
    assert pending is not None and pending.status == "resolved"
    assert rig.fake.replies == []
    assert not await rig.core.service.answer_approval("per_1", "deny")


async def test_single_and_multi_questions(rig: Rig) -> None:
    session = await rig.start()
    sid = session.harness_id
    one = {
        "question": "Which database?",
        "header": "DB",
        "options": [{"label": "postgres"}, {"label": "sqlite"}],
    }
    rig.fake.emit("question.asked", {"id": "que_1", "sessionID": sid, "questions": [one]})
    two = [{"question": "Name?"}, {"question": "Port?"}]
    rig.fake.emit("question.asked", {"id": "que_2", "sessionID": sid, "questions": two})
    rig.fake.emit("question.asked", {"id": "que_3", "sessionID": sid, "questions": [{"question": "Skip me"}]})
    await until(lambda: len(rig.rec.of(QuestionAsked)) == 3)
    first, second, _ = rig.rec.of(QuestionAsked)
    assert (first.question, first.options) == ("DB: Which database?", ("postgres", "sqlite"))
    assert second.question.startswith("1. Name?\n2. Port?") and second.options == ()
    await asyncio.sleep(0.02)
    assert await rig.core.service.answer_question("que_1", "postgres")
    assert await rig.core.service.answer_question("que_2", "api\n8080")
    assert await rig.core.service.answer_question("que_3", None)
    assert rig.fake.replies == [
        ("question:que_1", {"answers": [["postgres"]]}),
        ("question:que_2", {"answers": [["api"], ["8080"]]}),
        ("reject:que_3", None),
    ]


def _tool(sid: str, status: str, **state: object) -> dict[str, object]:
    part = {
        "id": "prt_tool",
        "sessionID": sid,
        "messageID": "m",
        "type": "tool",
        "tool": "bash",
        "callID": "call_1",
    }
    return {"sessionID": sid, "part": {**part, "state": {"status": status, **state}}}


async def test_tool_events_come_from_the_stream_unless_the_plugin_reports_them(rig: Rig) -> None:
    session = await rig.start()
    sid = session.harness_id
    rig.fake.emit("message.part.updated", _tool(sid, "running", input={"command": "make test"}))
    rig.fake.emit("message.part.updated", _tool(sid, "error", input={}, error="exit 2"))
    await until(lambda: rig.rec.of(ToolFinished))
    assert [(e.tool_id, e.name, e.detail) for e in rig.rec.of(ToolStarted)] == [
        ("call_1", "bash", "make test")
    ]
    assert [(e.ok, e.summary) for e in rig.rec.of(ToolFinished)] == [(False, "exit 2")]
    rig.rec.events.clear()
    await rig.core.registry.kv_set(f"opencode.server:{PRIMARY}/", "/w/proj")
    second = {**_tool(sid, "running"), "part": {**_tool(sid, "running")["part"], "callID": "call_2"}}  # type: ignore[dict-item]
    rig.fake.emit("message.part.updated", second)
    rig.fake.emit(
        "session.error",
        {"sessionID": sid, "error": {"name": "APIError", "data": {"message": "rate limited"}}},
    )
    await until(lambda: rig.rec.of(ErrorEvent))
    assert not rig.rec.of(ToolStarted)
    assert rig.rec.of(ErrorEvent)[0].message == "rate limited"


async def test_models_select_cancel_and_wake(rig: Rig) -> None:
    models = await rig.core.service.models("opencode")
    assert models[0].id == "github-copilot/gpt-5-mini" and models[0].default
    assert {m.id for m in models} == {
        "github-copilot/gpt-5-mini",
        "github-copilot/claude-x",
        "anthropic/sonnet",
    }
    assert models[0].label == "GitHub Copilot: GPT-5 mini"
    session = await rig.start()
    with pytest.raises(RelayError):
        await rig.core.service.set_model(session.session_key, "nope/none")
    await rig.core.service.set_model(session.session_key, "anthropic/sonnet")
    await rig.core.service.stop(session.session_key)
    assert rig.fake.aborted == [session.harness_id]
    rig.fake.ok_turn(session.harness_id, "done")
    await until(lambda: rig.rec.of(TurnCompleted))
    assert await rig.core.service.prompt(session.session_key, "and now the docs") == "sent"
    _, body, _ = rig.fake.prompts[-1]
    assert body["model"] == {"providerID": "anthropic", "modelID": "sonnet"}
    assert body["parts"] == [{"type": "text", "text": "and now the docs"}]


async def test_attached_files_reach_opencode_as_file_parts(rig: Rig) -> None:
    from agent_relay.models.attachments import Attachment

    session = await rig.start()
    rig.fake.ok_turn(session.harness_id, "done")
    await until(lambda: rig.rec.of(TurnCompleted))
    shot = Attachment(name="a.png", mime="image/png", size=1, local_path="/tmp/relay/a.png", sha256="0" * 64)
    assert await rig.core.service.prompt(session.session_key, "see this", (shot,)) == "sent"
    _, body, _ = rig.fake.prompts[-1]
    assert body["parts"] == [
        {"type": "text", "text": "see this"},
        {
            "type": "file",
            "mime": "image/png",
            "filename": "a.png",
            "url": Path("/tmp/relay/a.png").resolve().as_uri(),
        },
    ]


async def test_server_down_reports_unhealthy_and_start_fails_clearly(rig: Rig) -> None:
    assert await rig.adapter.health() == (True, f"opencode serve at {PRIMARY}")
    rig.fake.up = False
    rig.fake.close_streams()
    ok, detail = await rig.adapter.health()
    assert not ok and "not reachable" in detail
    with pytest.raises(AdapterUnavailableError):
        await rig.start()
    rig.fake.up = True
    await rig.connected()
    assert (await rig.adapter.health())[0]


async def test_wrong_password_is_unhealthy(registry: Registry) -> None:
    fake = FakeOpenCode(PRIMARY, password="right")
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        client = OpenCodeClient(http, PRIMARY, httpx.BasicAuth("opencode", "wrong"))
        assert not await client.healthy()
        assert fake.unauthorized == 1
        adapter = OpenCodeAdapter(OpenCodeConfig(url=PRIMARY), registry, "right", http)
        assert (await adapter.health())[0]


async def test_plugin_registered_server_is_followed_and_forgotten_when_gone(rig: Rig) -> None:
    await away.set_away(rig.core.registry, True)
    await rig.core.registry.kv_set(f"opencode.server:{OTHER}/", "/w/other")
    await until(lambda: rig.other.subscribers == 1)
    rig.other.ok_turn("ses_tui", "from the TUI")
    await until(lambda: rig.rec.of(TurnCompleted))
    session = await rig.core.registry.get_session("opencode:ses_tui")
    assert session is not None and session.extra["server"] == OTHER
    assert await rig.core.service.prompt("opencode:ses_tui", "keep going") == "sent"
    assert rig.other.prompts[-1][0] == "ses_tui" and not rig.fake.prompts
    rig.other.up = False
    rig.other.close_streams()

    async def forgotten() -> bool:
        return await rig.core.registry.kv_prefix("opencode.server:") == {}

    await until(forgotten)
