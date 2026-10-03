"""AC-30 approvals with an explicit deny at the relay's deadline, AC-31 parking, AC-32 loop guards,
plus steering, wake-up and the no-noise-at-the-desk rule."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay.adapters.base import Decision
from agent_relay.core import away
from agent_relay.core.parking import LIMIT_TEXT, RENEW_TEXT
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import (
    ApprovalRequested,
    Event,
    Harness,
    Origin,
    SessionParked,
    SessionStarted,
    TurnCompleted,
)
from agent_relay.models.sessions import SessionRecord, SessionStatus


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def of[T](self, kind: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, kind)]


@pytest.fixture
async def core(registry: Registry) -> AsyncIterator[tuple[Core, Recorder, list[str]]]:
    config = RelayConfig()
    config = config.model_copy(
        update={
            "approvals": config.approvals.model_copy(update={"deadline_s": 0.3}),
            "park": config.park.model_copy(update={"deadline_s": 1.0, "slice_s": 0.3}),
        }
    )
    built = Core(config, registry)
    recorder = Recorder()
    built.router.subscribe(recorder)
    notes: list[str] = []

    async def note(_: SessionRecord, text: str) -> None:
        notes.append(text)

    built.notify_targets.append(note)
    yield built, recorder, notes
    await built.router.stop()


async def hook(core: Core, family: str, event: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    reply = await core.handle_ipc({"op": "hook", "harness": family, "event": event, "payload": payload})
    await core.router.join()
    return reply  # type: ignore[return-value]


LOCAL_PRE: dict[str, JsonValue] = {
    "session_id": "L1",
    "cwd": "/w/proj",
    "tool_name": "run_in_terminal",
    "tool_input": {"command": "rm -rf build"},
    "tool_use_id": "t1",
}
CLI_PERM: dict[str, JsonValue] = {
    "sessionId": "C1",
    "cwd": "/w/proj",
    "toolName": "bash",
    "toolArgs": '{"command": "npm publish"}',
}
CURSOR_SHELL: dict[str, JsonValue] = {
    "conversation_id": "K1",
    "workspace_roots": ["/w/proj"],
    "command": "git push",
    "cwd": "/w/proj",
}
OC_PRE: dict[str, JsonValue] = {
    "sessionID": "ses_1",
    "callID": "call_1",
    "tool": "bash",
    "args": {"command": "rm x"},
    "directory": "/w/proj",
}


async def test_nothing_happens_at_the_desk(core: tuple[Core, Recorder, list[str]]) -> None:
    built, rec, _ = core
    cases: list[tuple[str, str, dict[str, JsonValue]]] = [
        ("copilot", "PreToolUse", LOCAL_PRE),
        ("cursor", "beforeShellExecution", CURSOR_SHELL),
        ("cursor", "stop", {"conversation_id": "K1", "workspace_roots": ["/w/proj"], "status": "completed"}),
    ]
    for family, event, payload in cases:
        assert await hook(built, family, event, payload) == {}
    assert rec.events == []
    assert await built.registry.list_sessions() == []


async def test_registers_session_and_auto_allows_read_only(core: tuple[Core, Recorder, list[str]]) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    reply = await hook(
        built, "copilot", "PreToolUse", {**LOCAL_PRE, "tool_name": "read_file", "tool_input": {"path": "a"}}
    )
    assert reply["stdout"]["permissionDecision"] == "allow"  # type: ignore[index,call-overload]
    sessions = await built.registry.list_sessions()
    assert [s.session_key for s in sessions] == ["vscode_local:L1"]
    assert rec.of(SessionStarted) and not rec.of(ApprovalRequested)


async def _answer_when_asked(built: Core, rec: Recorder, decision: Decision) -> None:
    for _ in range(200):
        if rec.of(ApprovalRequested):
            await built.service.answer_approval(rec.of(ApprovalRequested)[-1].request_id, decision)
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no approval requested")


@pytest.mark.parametrize(
    ("family", "event", "payload", "allow_shape", "deny_shape"),
    [
        ("copilot", "PreToolUse", LOCAL_PRE, {"permissionDecision": "allow"}, {"permissionDecision": "deny"}),
        ("copilot", "permissionRequest", CLI_PERM, {"behavior": "allow"}, {"behavior": "deny"}),
        (
            "copilot",
            "preToolUse",
            {**CLI_PERM},
            {"permissionDecision": "allow"},
            {"permissionDecision": "deny"},
        ),
        ("cursor", "beforeShellExecution", CURSOR_SHELL, {"permission": "allow"}, {"permission": "deny"}),
        ("opencode", "tool.execute.before", OC_PRE, {"decision": "allow"}, {"decision": "deny"}),
    ],
)
async def test_approval_round_trip_and_deadline_deny(
    core: tuple[Core, Recorder, list[str]],
    family: str,
    event: str,
    payload: dict[str, JsonValue],
    allow_shape: dict[str, str],
    deny_shape: dict[str, str],
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    answering = asyncio.create_task(_answer_when_asked(built, rec, "allow"))
    reply = await hook(built, family, event, payload)
    await answering
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and allow_shape.items() <= stdout.items()

    rec.events.clear()
    other: dict[str, JsonValue] = {**payload}
    for key in ("tool_input", "toolArgs", "command", "args"):
        if key in other:
            other[key] = "something else" if isinstance(other[key], str) else {"command": "other"}
    reply = await hook(built, family, event, other)
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and deny_shape.items() <= stdout.items()
    reason = (
        stdout.get("permissionDecisionReason")
        or stdout.get("message")
        or stdout.get("agent_message")
        or stdout.get("reason")
    )
    assert isinstance(reason, str) and "deadline" in reason
    assert built.config.approvals.deadline_s < built.config.approval_hook_timeout_s()


async def test_allow_for_session_skips_later_prompts(core: tuple[Core, Recorder, list[str]]) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    answering = asyncio.create_task(_answer_when_asked(built, rec, "allow_session"))
    await hook(built, "cursor", "beforeShellExecution", CURSOR_SHELL)
    await answering
    rec.events.clear()
    reply = await hook(built, "cursor", "beforeShellExecution", {**CURSOR_SHELL, "command": "ls -la"})
    assert reply["stdout"] == {"permission": "allow"}
    assert not rec.of(ApprovalRequested)


async def test_permission_request_after_approved_pre_tool_is_not_asked_twice(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    pre: dict[str, JsonValue] = {
        "session_id": "C9",
        "cwd": "/w/proj",
        "tool_name": "bash",
        "tool_input": {"command": "make"},
    }
    answering = asyncio.create_task(_answer_when_asked(built, rec, "allow"))
    await hook(built, "copilot", "PreToolUse", pre)
    await answering
    rec.events.clear()
    perm: dict[str, JsonValue] = {
        "session_id": "C9",
        "cwd": "/w/proj",
        "toolName": "bash",
        "tool_input": {"command": "make"},
    }
    reply = await hook(built, "copilot", "permissionRequest", perm)
    assert reply["stdout"] == {"behavior": "allow"}
    assert not rec.of(ApprovalRequested)


async def test_cursor_pre_tool_for_shell_defers_to_shell_hook(core: tuple[Core, Recorder, list[str]]) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    payload: dict[str, JsonValue] = {
        "conversation_id": "K2",
        "workspace_roots": ["/w"],
        "tool_name": "Shell",
        "tool_input": {"command": "x"},
    }
    assert await hook(built, "cursor", "preToolUse", payload) == {}


async def test_hook_failure_denies_approvals(
    core: tuple[Core, Recorder, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(built.broker, "request", broken)
    reply = await hook(built, "copilot", "PreToolUse", LOCAL_PRE)
    assert reply["stdout"]["permissionDecision"] == "deny"  # type: ignore[index,call-overload]


async def _session(built: Core, key: str) -> SessionRecord:
    found = await built.registry.get_session(key)
    assert found is not None
    return found


async def _wait_status(built: Core, key: str, status: SessionStatus) -> None:
    for _ in range(300):
        found = await built.registry.get_session(key)
        if found is not None and found.status is status:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"{key} never reached {status}")


STOP_LOCAL: dict[str, JsonValue] = {"session_id": "L1", "cwd": "/w/proj", "stop_hook_active": False}
STOP_CURSOR: dict[str, JsonValue] = {
    "conversation_id": "K1",
    "workspace_roots": ["/w/proj"],
    "status": "completed",
    "loop_count": 0,
}


async def test_park_continues_with_phone_message(
    core: tuple[Core, Recorder, list[str]], tmp_path: Path
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        '{"role": "user", "content": "go"}\n{"role": "assistant", "content": "All done."}\n'
    )
    task = asyncio.create_task(
        hook(built, "copilot", "Stop", {**STOP_LOCAL, "transcript_path": str(transcript)})
    )
    await _wait_status(built, "vscode_local:L1", SessionStatus.PARKED)
    receipt = await built.service.prompt("vscode_local:L1", "now add tests")
    reply = await task
    assert receipt == "delivered to the parked session"
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and stdout["decision"] == "block"
    assert "now add tests" in str(stdout["reason"])
    assert rec.of(TurnCompleted)[0].text == "All done."
    assert rec.of(SessionParked)
    assert (await _session(built, "vscode_local:L1")).status is SessionStatus.RUNNING


async def test_cursor_park_uses_followup_and_last_response(core: tuple[Core, Recorder, list[str]]) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    await hook(
        built,
        "cursor",
        "afterAgentResponse",
        {"conversation_id": "K1", "workspace_roots": ["/w/proj"], "text": "Fixed it."},
    )
    task = asyncio.create_task(hook(built, "cursor", "stop", STOP_CURSOR))
    await _wait_status(built, "cursor:K1", SessionStatus.PARKED)
    await built.service.prompt("cursor:K1", "ship it")
    reply = await task
    assert reply["stdout"] == {"followup_message": "Message from the user, sent from their phone:\n\nship it"}
    assert rec.of(TurnCompleted)[-1].text == "Fixed it."


@pytest.mark.parametrize("how", ["back", "panic", "away_off"])
async def test_park_releases(core: tuple[Core, Recorder, list[str]], how: str) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    task = asyncio.create_task(hook(built, "cursor", "stop", STOP_CURSOR))
    await _wait_status(built, "cursor:K1", SessionStatus.PARKED)
    if how == "back":
        await built.service.back("cursor:K1")
    elif how == "panic":
        await built.service.panic()
    else:
        await built.service.set_away(False)
    assert await asyncio.wait_for(task, 2) == {}
    assert (await _session(built, "cursor:K1")).status is SessionStatus.IDLE


async def test_park_renews_slices_then_hits_deadline(core: tuple[Core, Recorder, list[str]]) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    replies = []
    for _ in range(10):
        reply = await asyncio.wait_for(hook(built, "cursor", "stop", STOP_CURSOR), 2)
        replies.append(reply)
        if reply == {}:
            break
    renewals = [r for r in replies if r and r["stdout"] == {"followup_message": RENEW_TEXT}]
    assert len(renewals) >= 2 and replies[-1] == {}
    assert len(rec.of(SessionParked)) == 1
    assert len(rec.of(TurnCompleted)) == 1
    assert (await _session(built, "cursor:K1")).status is SessionStatus.IDLE


async def test_copilot_block_cap_posts_limit(core: tuple[Core, Recorder, list[str]]) -> None:
    built, _, notes = core
    await away.set_away(built.registry, True)
    stop: dict[str, JsonValue] = {"sessionId": "C2", "cwd": "/w/proj", "stopReason": "end_turn"}
    cap = built.config.park.copilot_max_blocks
    blocked = 0
    for _ in range(cap + 2):
        task = asyncio.create_task(hook(built, "copilot", "agentStop", stop))
        await asyncio.sleep(0.02)
        session = await built.registry.get_session("copilot_cli:C2")
        if session is not None and session.status is SessionStatus.PARKED:
            await built.service.prompt("copilot_cli:C2", "again")
        reply = await asyncio.wait_for(task, 2)
        if reply == {}:
            break
        blocked += 1
    assert blocked == cap - 1
    assert notes == [LIMIT_TEXT]
    assert (await _session(built, "copilot_cli:C2")).extra["wake_needed"] is True


async def test_stop_hook_active_without_our_block_is_left_alone(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    assert await hook(built, "copilot", "Stop", {**STOP_LOCAL, "stop_hook_active": True}) == {}


async def test_steering_goes_out_on_next_post_tool(core: tuple[Core, Recorder, list[str]]) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    await hook(built, "copilot", "PreToolUse", {**LOCAL_PRE, "tool_name": "read_file"})
    receipt = await built.service.prompt("vscode_local:L1", "use pytest, not unittest")
    assert receipt.startswith("queued as steering")
    reply = await hook(
        built, "copilot", "PostToolUse", {**LOCAL_PRE, "tool_name": "read_file", "tool_response": "ok"}
    )
    assert reply["stdout"] == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "Message from the user, sent from their phone:\n\nuse pytest, not unittest",
        }
    }
    again = await hook(built, "copilot", "PostToolUse", {**LOCAL_PRE, "tool_name": "read_file"})
    assert again == {}


async def test_agent_idle_notification_wakes_with_queued_text(core: tuple[Core, Recorder, list[str]]) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    await built.registry.upsert_session(
        SessionRecord(
            session_key="copilot_cli:C3",
            harness=Harness.COPILOT_CLI,
            harness_id="C3",
            workspace="/w/proj",
            status=SessionStatus.IDLE,
        )
    )
    await built.service.prompt("copilot_cli:C3", "wake up")
    reply = await hook(
        built,
        "copilot",
        "notification",
        {"session_id": "C3", "cwd": "/w/proj", "notification_type": "agent_idle", "message": "idle"},
    )
    assert reply["stdout"] == {
        "additionalContext": "Message from the user, sent from their phone:\n\nwake up"
    }


async def test_opencode_register_records_server(core: tuple[Core, Recorder, list[str]]) -> None:
    built, _, _ = core
    await hook(
        built, "opencode", "plugin.register", {"directory": "/w", "serverUrl": "http://127.0.0.1:4096/"}
    )
    await hook(
        built, "opencode", "plugin.register", {"directory": "/v", "serverUrl": "http://127.0.0.1:5000/"}
    )
    assert await built.registry.kv_prefix("opencode.server:") == {
        "opencode.server:http://127.0.0.1:4096/": "/w",
        "opencode.server:http://127.0.0.1:5000/": "/v",
    }
    assert await built.registry.kv_prefix("opencode.server_") == {}


async def test_bad_messages_fail_open(core: tuple[Core, Recorder, list[str]]) -> None:
    built, _, _ = core
    assert await hook(built, "copilot", "madeUp", {}) == {}
    assert await built.handle_ipc({"op": "hook", "harness": "nope", "event": "x", "payload": {}}) == {}
    assert await built.handle_ipc({"op": "hook"}) == {}
    assert "unknown op" in str(await built.handle_ipc({"op": "zzz"}))


async def test_restart_recovers_parked_and_expires_pending(core: tuple[Core, Recorder, list[str]]) -> None:
    built, _, _ = core
    await built.registry.upsert_session(
        SessionRecord(
            session_key="cursor:Z", harness=Harness.CURSOR, harness_id="Z", status=SessionStatus.PARKED
        )
    )
    assert built.parking is not None
    assert await built.parking.recover() == 1
    assert (await _session(built, "cursor:Z")).status is SessionStatus.IDLE


async def test_phone_sessions_are_gated_even_with_away_off(core: tuple[Core, Recorder, list[str]]) -> None:
    built, rec, _ = core
    await built.registry.upsert_session(
        SessionRecord(
            session_key="opencode:ses_p",
            harness=Harness.OPENCODE,
            harness_id="ses_p",
            workspace="/w/proj",
            origin=Origin.PHONE,
            status=SessionStatus.RUNNING,
        )
    )
    payload: dict[str, JsonValue] = {**OC_PRE, "sessionID": "ses_p"}
    answering = asyncio.create_task(_answer_when_asked(built, rec, "deny"))
    reply = await hook(built, "opencode", "tool.execute.before", payload)
    await answering
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and stdout["decision"] == "deny"
    assert await built.registry.kv_get("approved_call:call_1") == "deny"
