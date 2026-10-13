"""AC-21: the Cursor CLI adapter against a fake `cursor-agent` that speaks ACP over stdio."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent_relay.adapters.base import AdapterUnavailableError
from agent_relay.adapters.cursor_cli.adapter import IDLE_S, RESUME_EXTRA, STOPPED_TEXT, CursorCliAdapter
from agent_relay.adapters.cursor_cli.catalog import LOGIN_HINT, CursorCli
from agent_relay.adapters.cursor_cli.rpc import AcpProcess, NotificationHandler, RequestHandler
from agent_relay.core import away
from agent_relay.core.hooks import APPROVED_CALL_KEY
from agent_relay.core.registry import Registry
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
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
)
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import save_workspaces

FAKE = Path(__file__).resolve().parents[1] / "fakes" / "acp_agent.py"


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def of[T](self, kind: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, kind)]


async def until(check: Callable[[], object], timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        result = check()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


@dataclass
class Rig:
    core: Core
    adapter: CursorCliAdapter
    rec: Recorder
    registry: Registry
    workspace: str
    fake_dir: Path

    def configure(self, **flags: bool) -> None:
        (self.fake_dir / "config.json").write_text(json.dumps(flags))

    def received(self) -> list[dict[str, Any]]:
        log = self.fake_dir / "acp.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def methods(self) -> list[str]:
        return [m["method"] for m in self.received() if "method" in m]

    def replies(self) -> list[dict[str, Any]]:
        return [m for m in self.received() if "method" not in m]

    def runs(self) -> list[list[str]]:
        log = self.fake_dir / "argv.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    async def start(self, prompt: str = "Reply with exactly: OK", model: str | None = None) -> SessionRecord:
        return await self.core.service.start("cursor", "proj", prompt, model)

    async def session(self, key: str) -> SessionRecord:
        found = await self.registry.get_session(key)
        assert found is not None
        return found

    async def turns(self, count: int) -> list[TurnCompleted]:
        await until(lambda: len(self.rec.of(TurnCompleted)) >= count)
        return self.rec.of(TurnCompleted)

    async def asked[T](self, kind: type[T], count: int = 1) -> T:
        await until(lambda: len(self.rec.of(kind)) >= count)
        await self.core.router.join()
        return self.rec.of(kind)[count - 1]


@pytest.fixture
async def rig(
    registry: Registry, relay_home: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[Rig]:
    idle_s: float = getattr(request, "param", IDLE_S)
    fake_dir = relay_home / "acp"
    fake_dir.mkdir()
    monkeypatch.setenv("FAKE_ACP_DIR", str(fake_dir))
    shim = relay_home / "cursor-agent"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    shim.chmod(0o755)
    workspace = relay_home / "ws"
    workspace.mkdir()
    save_workspaces(WorkspacesConfig(workspaces=[Workspace(name="proj", path=str(workspace))]))
    core = Core(RelayConfig(), registry)
    rec = Recorder()
    core.router.subscribe(rec)
    adapter = CursorCliAdapter(registry, CursorCli(shim), init_timeout=10, idle_s=idle_s)
    core.router.add_adapter(adapter)
    yield Rig(core, adapter, rec, registry, str(workspace), fake_dir)
    await adapter.aclose()
    await core.router.stop()


# sessions and turns


async def test_phone_session_streams_ok(rig: Rig) -> None:
    record = await rig.start()
    (done,) = await rig.turns(1)
    assert done.text == "OK"
    assert [e.text for e in rig.rec.of(TextDelta)] == ["O", "K"]
    assert record.harness is Harness.CURSOR_CLI
    assert record.origin is Origin.PHONE
    assert record.harness_id.startswith("acp-")
    assert (
        record.extra[RESUME_EXTRA]
        == f"Cursor CLI. Resume at the desk with `agent resume {record.harness_id}`"
    )
    assert rig.methods() == ["initialize", "authenticate", "session/new", "session/prompt"]
    (initialize, _, new, prompt, *_) = rig.received()
    assert initialize["params"]["clientCapabilities"] == {
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }
    assert new["params"] == {"cwd": rig.workspace, "mcpServers": []}
    assert prompt["params"]["prompt"] == [{"type": "text", "text": "Reply with exactly: OK"}]
    assert rig.runs()[-1] == ["--workspace", rig.workspace, "acp"]
    assert (await rig.session(record.session_key)).status is SessionStatus.IDLE


async def test_a_chosen_model_is_passed_on_the_command_line(rig: Rig) -> None:
    await rig.start(model="gpt-5")
    await rig.turns(1)
    assert rig.runs()[-1] == ["--workspace", rig.workspace, "--model", "gpt-5", "acp"]


async def test_logged_out_cli_is_reported_before_spawning(rig: Rig) -> None:
    rig.configure(logged_out=True)
    with pytest.raises(AdapterUnavailableError, match="cursor-agent login"):
        await rig.start()
    assert rig.runs() == [["status"]]
    assert await rig.adapter.health() == (False, LOGIN_HINT)


async def test_failed_authentication_asks_for_a_login(rig: Rig) -> None:
    rig.configure(auth_fail=True)
    with pytest.raises(AdapterUnavailableError, match="cursor-agent login"):
        await rig.start()
    assert rig.methods() == ["initialize", "authenticate"]


async def test_health_reports_the_login(rig: Rig) -> None:
    ok, detail = await rig.adapter.health()
    assert ok and "dev@example.com" in detail


async def test_an_idle_session_takes_the_next_prompt(rig: Rig) -> None:
    record = await rig.start()
    await rig.turns(1)
    assert await rig.core.service.prompt(record.session_key, "again") == "sent"
    second = (await rig.turns(2))[1]
    assert second.text == "OK"
    assert rig.methods().count("session/new") == 1
    assert rig.methods().count("session/prompt") == 2


async def test_a_turn_error_reaches_the_phone(rig: Rig) -> None:
    record = await rig.start("please fail")
    await until(lambda: rig.rec.of(ErrorEvent))
    assert "model overloaded" in rig.rec.of(ErrorEvent)[0].message
    await until(lambda: _status(rig, record, SessionStatus.IDLE))


async def test_an_unusual_stop_reason_is_shown(rig: Rig) -> None:
    await rig.start("hit the limit")
    (done,) = await rig.turns(1)
    assert done.text == "partial\n\n(Cursor stopped: max tokens)"


async def _status(rig: Rig, record: SessionRecord, status: SessionStatus) -> bool:
    return (await rig.session(record.session_key)).status is status


async def test_a_crashed_process_is_resumed_on_the_next_prompt(rig: Rig) -> None:
    record = await rig.start("crash now")
    await until(lambda: rig.rec.of(ErrorEvent))
    assert "exited (3)" in rig.rec.of(ErrorEvent)[0].message
    await until(lambda: _status(rig, record, SessionStatus.IDLE))
    assert await rig.core.service.prompt(record.session_key, "Reply with exactly: OK") == "sent"
    (done,) = await rig.turns(1)
    assert done.text == "OK"
    load = next(m for m in rig.received() if m.get("method") == "session/load")
    assert load["params"] == {"sessionId": record.harness_id, "cwd": rig.workspace, "mcpServers": []}
    assert "OLD HISTORY" not in [e.text for e in rig.rec.of(TextDelta)]


async def test_a_restarted_relay_resumes_with_session_load(rig: Rig) -> None:
    record = await rig.start()
    await rig.turns(1)
    await rig.adapter.aclose()
    await rig.core.service.prompt(record.session_key, "Reply with exactly: OK")
    await rig.turns(2)
    assert rig.methods().count("session/load") == 1


async def test_resume_needs_load_support(rig: Rig) -> None:
    record = await rig.start()
    await rig.turns(1)
    await rig.adapter.aclose()
    rig.configure(no_load=True)
    with pytest.raises(AdapterUnavailableError, match="agent resume"):
        await rig.adapter.send_prompt(await rig.session(record.session_key), "again")


# approvals, questions, plans


@pytest.mark.parametrize(
    ("decision", "option", "ok"),
    [
        ("allow", "allow-once", True),
        ("allow_session", "allow-always", True),
        ("allow_workspace", "allow-once", True),
        ("deny", "reject-once", False),
    ],
)
async def test_permission_requests_are_answered_from_the_phone(
    rig: Rig, decision: str, option: str, ok: bool
) -> None:
    await rig.start("use a tool")
    ask = await rig.asked(ApprovalRequested)
    assert (ask.tool, ask.detail) == ("Run ls", "ls")
    assert await rig.core.service.answer_approval(ask.request_id, decision)  # type: ignore[arg-type]
    (done,) = await rig.turns(1)
    assert done.text == f"tool {option}"
    reply = rig.replies()[-1]
    assert reply["result"] == {"outcome": {"outcome": "selected", "optionId": option}}
    (started,) = rig.rec.of(ToolStarted)
    (finished,) = rig.rec.of(ToolFinished)
    assert (started.tool_id, started.name) == ("tc1", "Run ls")
    assert (finished.ok, finished.summary) == (ok, "file.txt")


async def test_a_deny_reason_is_dropped_because_acp_has_no_field_for_it(rig: Rig) -> None:
    await rig.start("use a tool")
    ask = await rig.asked(ApprovalRequested)
    assert await rig.core.service.answer_approval(ask.request_id, "deny", reason="not in prod")
    (done,) = await rig.turns(1)
    assert done.text == "tool reject-once"
    assert rig.replies()[-1]["result"] == {"outcome": {"outcome": "selected", "optionId": "reject-once"}}


async def test_an_earlier_phone_decision_for_the_call_is_reused(rig: Rig) -> None:
    await rig.registry.kv_set(APPROVED_CALL_KEY.format(call_id="tc1"), "allow")
    await rig.start("use a tool")
    (done,) = await rig.turns(1)
    assert done.text == "tool allow-once"
    assert rig.rec.of(ApprovalRequested) == []


async def test_a_question_is_answered_by_number(rig: Rig) -> None:
    await rig.start("ask me")
    ask = await rig.asked(QuestionAsked)
    assert ask.question == "Need input\nWhich mode?"
    assert ask.options == ("Agent", "Plan")
    assert await rig.core.service.answer_question(ask.request_id, "2")
    (done,) = await rig.turns(1)
    assert json.loads(done.text or "") == {
        "outcome": "answered",
        "answers": [{"questionId": "q1", "selectedOptionIds": ["plan"]}],
    }


async def test_several_questions_take_one_line_each(rig: Rig) -> None:
    await rig.start("ask twice")
    ask = await rig.asked(QuestionAsked)
    assert ask.options == ()
    assert "1) Which mode? [1. Agent, 2. Plan]" in ask.question
    await rig.core.service.answer_question(ask.request_id, "agent\nA, b")
    (done,) = await rig.turns(1)
    assert json.loads(done.text or "")["answers"] == [
        {"questionId": "q1", "selectedOptionIds": ["agent"]},
        {"questionId": "q2", "selectedOptionIds": ["a", "b"]},
    ]


async def test_a_skipped_question_is_reported_as_skipped(rig: Rig) -> None:
    await rig.start("ask me")
    ask = await rig.asked(QuestionAsked)
    await rig.core.service.answer_question(ask.request_id, None, by="deadline")
    (done,) = await rig.turns(1)
    assert json.loads(done.text or "")["outcome"] == "skipped"


@pytest.mark.parametrize(("decision", "outcome"), [("allow", "accepted"), ("deny", "rejected")])
async def test_plans_need_phone_approval(rig: Rig, decision: str, outcome: str) -> None:
    await rig.start("plan first")
    ask = await rig.asked(ApprovalRequested)
    assert ask.tool == "plan"
    assert ask.detail == "Refactor\n\n1. a\n2. b"
    await rig.core.service.answer_approval(ask.request_id, decision)  # type: ignore[arg-type]
    (done,) = await rig.turns(1)
    assert done.text == f"plan {outcome}"


# cancel and steering


async def test_stop_cancels_the_turn(rig: Rig) -> None:
    record = await rig.start("wait for me")
    await until(lambda: rig.rec.of(TextDelta))
    await rig.core.service.stop(record.session_key)
    (done,) = await rig.turns(1)
    assert done.text == f"working\n\n{STOPPED_TEXT}"
    assert "session/cancel" in rig.methods()


async def test_stop_closes_an_open_approval(rig: Rig) -> None:
    record = await rig.start("use a tool")
    ask = await rig.asked(ApprovalRequested)
    await rig.core.service.stop(record.session_key)
    resolved = await rig.asked(ApprovalResolved)
    assert (resolved.request_id, resolved.by) == (ask.request_id, "desk")
    pending = await rig.registry.get_pending(ask.request_id)
    assert pending is not None and pending.status == "resolved"
    await rig.turns(1)


async def test_a_prompt_during_a_turn_runs_when_the_turn_ends(rig: Rig) -> None:
    record = await rig.start("use a tool")
    ask = await rig.asked(ApprovalRequested)
    receipt = await rig.core.service.prompt(record.session_key, "Reply with exactly: OK")
    assert receipt == "Queued (1 waiting)"
    await rig.core.service.answer_approval(ask.request_id, "allow")
    first, second = await rig.turns(2)
    assert first.text == "tool allow-once"
    assert second.text == "OK"
    assert await rig.registry.pop_queued(record.session_key, ("steer", "prompt")) == []


# models


async def test_models_are_parsed_and_cached(rig: Rig) -> None:
    first = await rig.adapter.list_models()
    second = await rig.adapter.list_models()
    assert [(m.id, m.default) for m in first] == [("auto", True), ("gpt-5", False), ("sonnet-4", False)]
    assert first == second
    assert sum(1 for run in rig.runs() if run == ["models"]) == 1


async def test_set_model_uses_session_set_model(rig: Rig) -> None:
    record = await rig.start()
    await rig.turns(1)
    await rig.core.service.set_model(record.session_key, "gpt-5")
    assert rig.received()[-1]["params"] == {"sessionId": record.harness_id, "modelId": "gpt-5"}


async def test_set_model_restarts_the_process_when_unsupported(rig: Rig) -> None:
    rig.configure(set_model_fail=True)
    record = await rig.start()
    await rig.turns(1)
    await rig.core.service.set_model(record.session_key, "sonnet-4")
    await rig.core.service.prompt(record.session_key, "Reply with exactly: OK")
    await rig.turns(2)
    assert rig.runs()[-1] == ["--workspace", rig.workspace, "--model", "sonnet-4", "acp"]
    assert "session/load" in rig.methods()


async def test_an_unknown_model_is_refused(rig: Rig) -> None:
    record = await rig.start()
    await rig.turns(1)
    with pytest.raises(AdapterUnavailableError, match="no model `nope`"):
        await rig.core.service.set_model(record.session_key, "nope")


# hooks


async def test_cursor_hooks_leave_acp_sessions_alone(rig: Rig) -> None:
    record = await rig.start()
    await rig.turns(1)
    await away.set_away(rig.registry, True)
    payload = {"conversation_id": record.harness_id, "command": "git push", "cwd": rig.workspace}
    reply = await rig.core.handle_ipc(
        {"op": "hook", "harness": "cursor", "event": "beforeShellExecution", "payload": payload}
    )
    assert reply == {}
    assert rig.rec.of(ApprovalRequested) == []


# process lifetime and locking


@pytest.mark.parametrize("rig", [0.4], indirect=True)
async def test_an_idle_session_process_is_closed_and_resumed_by_the_next_prompt(rig: Rig) -> None:
    record = await rig.start()
    await rig.turns(1)
    await until(lambda: record.session_key not in rig.adapter._live)
    assert rig.methods().count("session/load") == 0
    assert await rig.core.service.prompt(record.session_key, "Reply with exactly: OK") == "sent"
    second = (await rig.turns(2))[1]
    assert second.text == "OK"
    assert rig.methods().count("session/load") == 1


@pytest.mark.parametrize("rig", [0.4], indirect=True)
async def test_a_session_with_an_open_request_is_not_reaped(rig: Rig) -> None:
    record = await rig.start("use a tool")
    ask = await rig.asked(ApprovalRequested)
    await asyncio.sleep(1.0)
    assert record.session_key in rig.adapter._live
    await rig.core.service.answer_approval(ask.request_id, "allow")
    (done,) = await rig.turns(1)
    assert done.text == "tool allow-once"


async def test_a_slow_spawn_for_one_session_does_not_block_another(rig: Rig) -> None:
    one = await rig.start()
    two = await rig.start()
    await rig.turns(2)
    await rig.adapter.aclose()
    release = asyncio.Event()
    calls: list[int] = []
    real = rig.adapter._spawn

    async def spawn(
        argv: Sequence[str], cwd: Path, on_request: RequestHandler, on_notification: NotificationHandler
    ) -> AcpProcess:
        calls.append(1)
        if len(calls) == 1:
            await release.wait()
        return await real(argv, cwd, on_request, on_notification)

    rig.adapter._spawn = spawn
    first = asyncio.create_task(rig.adapter.send_prompt(await rig.session(one.session_key), "again"))
    await until(lambda: calls and rig.adapter._locks[one.session_key].locked())
    await asyncio.wait_for(rig.adapter.send_prompt(await rig.session(two.session_key), "again"), 5)
    assert not first.done()
    release.set()
    await first


async def test_an_unexpected_turn_failure_frees_the_session_and_drops_the_process(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = await rig.start()
    await rig.turns(1)
    live = rig.adapter._live[record.session_key]

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(live.process, "request", boom)
    assert await rig.core.service.prompt(record.session_key, "again") == "sent"
    await until(lambda: rig.rec.of(ErrorEvent))
    assert rig.rec.of(ErrorEvent)[0].message == "boom"
    await until(lambda: _status(rig, record, SessionStatus.IDLE))
    await until(lambda: record.session_key not in rig.adapter._live)
    assert await rig.core.service.prompt(record.session_key, "Reply with exactly: OK") == "sent"
    assert (await rig.turns(2))[1].text == "OK"
    assert rig.methods().count("session/load") == 1
