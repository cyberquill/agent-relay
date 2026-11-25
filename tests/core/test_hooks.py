"""AC-30 tool calls the relay answers are allowed at once, AC-31 parking, AC-32 loop guards,
plus steering, wake-up and the no-noise-at-the-desk rule."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.claims import CURSOR_SCOPE, make_claim, take_claim
from agent_relay.core.hooks import ALLOW_REASON, HOOKED
from agent_relay.core.parking import PHONE_PREFIX, RENEW_TEXT
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.hook.outputs import ECHO_INSTRUCTION
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import (
    ApprovalRequested,
    Event,
    Harness,
    Origin,
    SessionParked,
    SessionReleased,
    SessionStarted,
    ToolStarted,
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


async def test_a_desk_session_is_registered_while_away_and_its_tool_call_allowed(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    reply = await hook(
        built, "copilot", "PreToolUse", {**LOCAL_PRE, "tool_name": "read_file", "tool_input": {"path": "a"}}
    )
    assert reply["stdout"]["permissionDecision"] == "allow"  # type: ignore[index,call-overload]
    sessions = await built.registry.list_sessions()
    assert [s.session_key for s in sessions] == ["vscode:L1"]
    assert rec.of(SessionStarted) and not rec.of(ApprovalRequested)


@pytest.mark.parametrize(
    ("family", "event", "payload", "allow_shape"),
    [
        ("copilot", "PreToolUse", LOCAL_PRE, {"permissionDecision": "allow"}),
        ("copilot", "permissionRequest", CLI_PERM, {"behavior": "allow"}),
        ("copilot", "preToolUse", {**CLI_PERM}, {"permissionDecision": "allow"}),
        ("cursor", "beforeShellExecution", CURSOR_SHELL, {"permission": "allow"}),
        ("opencode", "tool.execute.before", OC_PRE, {"decision": "allow"}),
    ],
)
async def test_a_tool_call_is_allowed_at_once_in_every_harness_format(
    core: tuple[Core, Recorder, list[str]],
    family: str,
    event: str,
    payload: dict[str, JsonValue],
    allow_shape: dict[str, str],
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    reply = await asyncio.wait_for(hook(built, family, event, payload), 1)
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and allow_shape.items() <= stdout.items()
    assert not rec.of(ApprovalRequested) and await built.registry.open_pending() == []


async def test_a_phone_session_is_still_allowed_after_away_goes_off(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    await built.registry.create_session(
        SessionRecord(
            session_key="opencode:ses_p",
            harness=Harness.OPENCODE,
            harness_id="ses_p",
            workspace="/w/proj",
            origin=Origin.PHONE,
            status=SessionStatus.RUNNING,
        )
    )
    await away.set_away(built.registry, True)
    await built.service.set_away(False)
    reply = await hook(built, "opencode", "tool.execute.before", {**OC_PRE, "sessionID": "ses_p"})
    assert reply["stdout"] == {"decision": "allow", "reason": ALLOW_REASON}
    assert not rec.of(ApprovalRequested)


async def _phone_session(built: Core, harness: Harness, sid: str, **fields: JsonValue) -> str:
    record = SessionRecord(
        session_key=f"{harness.value}:{sid}",
        harness=harness,
        harness_id=sid,
        workspace="/w/proj",
        origin=Origin.PHONE,
        status=SessionStatus.RUNNING,
    )
    await built.registry.create_session(record.model_copy(update=fields))
    return record.session_key


async def test_away_off_hands_a_vscode_phone_session_to_the_desk(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, _, _ = core
    key = await _phone_session(built, Harness.VSCODE, "L1")
    reply = await hook(built, "copilot", "PreToolUse", LOCAL_PRE)
    assert reply["stdout"]["permissionDecision"] == "allow"  # type: ignore[index,call-overload]
    assert "Handed 1 phone session(s) to the desk" in await built.service.set_away(False)
    session = await _session(built, key)
    assert session.extra[away.AT_DESK] is True and not away.phone_held(session)
    assert await hook(built, "copilot", "PreToolUse", {**LOCAL_PRE, "tool_use_id": "t2"}) == {}
    assert "Handed" not in await built.service.set_away(False)


async def test_back_in_a_thread_hands_only_a_desk_visible_phone_session_to_the_desk(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, _, _ = core
    visible = await _phone_session(built, Harness.VSCODE_COPILOT, "A1")
    headless = await _phone_session(built, Harness.OPENCODE, "ses_p")
    assert await built.service.back(visible) == "Handed to the desk."
    assert await built.service.back(headless) == "That session is not parked."
    assert not away.phone_held(await _session(built, visible))
    assert away.phone_held(await _session(built, headless))


async def test_away_off_is_global_and_ignores_the_workspace_it_is_given(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, _, _ = core
    multi = await _phone_session(
        built, Harness.VSCODE, "M1", workspace="/w/one", extra={away.FOLDERS: ["/w/one", "/w/two"]}
    )
    other = await _phone_session(built, Harness.VSCODE, "O1", workspace="/w/three")
    assert "Handed 2 phone session(s)" in await built.service.set_away_for(False, "/w/two")
    assert not away.phone_held(await _session(built, multi))
    assert not away.phone_held(await _session(built, other))


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


async def test_a_hook_failure_leaves_the_tool_call_to_the_app(
    core: tuple[Core, Recorder, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(built.hooks, "_publish_tool", broken)
    assert await hook(built, "copilot", "PreToolUse", LOCAL_PRE) == {}


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
    await _wait_status(built, "vscode:L1", SessionStatus.PARKED)
    receipt = await built.service.prompt("vscode:L1", "now add tests")
    reply = await task
    assert receipt == "delivered to the parked session"
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and stdout["decision"] == "block"
    assert "now add tests" in str(stdout["reason"])
    assert rec.of(TurnCompleted)[0].text == "All done."
    assert rec.of(SessionParked)
    assert (await _session(built, "vscode:L1")).status is SessionStatus.RUNNING


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
    assert reply["stdout"] == {"followup_message": f"{PHONE_PREFIX}ship it"}
    assert rec.of(TurnCompleted)[-1].text == "Fixed it."


async def test_a_cursor_stop_without_response_text_reads_the_reply_from_the_chat_transcript(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    """Live 2026-11-22: a Cursor CLI chat sent no `afterAgentResponse` text, so the phone got `Done.`."""
    built, rec, _ = core
    await away.set_away(built.registry, True)
    folder = paths.cursor_dir() / "projects" / "w-proj" / "agent-transcripts" / "K1"
    folder.mkdir(parents=True)
    rows = [
        {"role": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "Fixed it."}]}},
    ]
    (folder / "K1.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    task = asyncio.create_task(hook(built, "cursor", "stop", STOP_CURSOR))
    await _wait_status(built, "cursor:K1", SessionStatus.PARKED)
    await built.service.back("cursor:K1")
    await asyncio.wait_for(task, 2)
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


async def test_copilot_block_cap_ends_parking_without_a_notice(
    core: tuple[Core, Recorder, list[str]],
) -> None:
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
    assert blocked == cap
    assert notes == [], "hitting the parking limit is logged, not posted to the phone"
    assert "wake_needed" not in (await _session(built, "copilot_cli:C2")).extra


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
    await built.service.set_queue_mode("vscode:L1", "steer")
    receipt = await built.service.prompt("vscode:L1", "use pytest, not unittest")
    assert receipt.startswith("queued as steering")
    reply = await hook(
        built, "copilot", "PostToolUse", {**LOCAL_PRE, "tool_name": "read_file", "tool_response": "ok"}
    )
    assert reply["stdout"] == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"{PHONE_PREFIX}use pytest, not unittest",
        },
        "systemMessage": "📱 use pytest, not unittest",
    }
    again = await hook(built, "copilot", "PostToolUse", {**LOCAL_PRE, "tool_name": "read_file"})
    assert again == {}


async def test_agent_idle_notification_wakes_with_queued_text(core: tuple[Core, Recorder, list[str]]) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    await built.registry.create_session(
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
        "additionalContext": f"{PHONE_PREFIX}wake up\n\n{ECHO_INSTRUCTION}\n📱 wake up",
        "systemMessage": "📱 wake up",
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
    await built.registry.create_session(
        SessionRecord(
            session_key="cursor:Z", harness=Harness.CURSOR, harness_id="Z", status=SessionStatus.PARKED
        )
    )
    assert built.parking is not None
    assert await built.parking.recover() == 1
    assert (await _session(built, "cursor:Z")).status is SessionStatus.IDLE


async def test_a_chat_the_phone_took_over_parks_at_its_stop_even_with_away_off(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    """D146: arming a running desk chat holds its next stop, and the phone's reply continues the turn."""
    built, rec, _ = core
    await built.registry.create_session(
        SessionRecord(
            session_key="cursor:K1",
            harness=Harness.CURSOR,
            harness_id="K1",
            workspace="/w/proj",
            origin=Origin.DESK,
            status=SessionStatus.RUNNING,
            extra={away.TAKEN_OVER: True, away.ARMED: True},
        )
    )
    task = asyncio.create_task(hook(built, "cursor", "stop", STOP_CURSOR))
    await _wait_status(built, "cursor:K1", SessionStatus.PARKED)
    assert (
        await built.service.prompt("cursor:K1", "also add a changelog") == "delivered to the parked session"
    )
    reply = await asyncio.wait_for(task, 2)
    assert "also add a changelog" in str(reply["stdout"]["followup_message"])  # type: ignore[index,call-overload]
    assert rec.of(SessionParked)


async def test_a_taken_over_chat_handed_back_no_longer_parks_with_away_off(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    await built.registry.create_session(
        SessionRecord(
            session_key="cursor:K1",
            harness=Harness.CURSOR,
            harness_id="K1",
            workspace="/w/proj",
            origin=Origin.DESK,
            status=SessionStatus.RUNNING,
            extra={away.TAKEN_OVER: True, away.ARMED: True},
        )
    )
    await built.service.back("cursor:K1")
    assert await asyncio.wait_for(hook(built, "cursor", "stop", STOP_CURSOR), 2) == {}
    assert not rec.of(SessionParked)


async def test_a_phone_session_with_away_off_ends_its_turn_idle_without_parking(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    await built.registry.create_session(
        SessionRecord(
            session_key="cursor:K1",
            harness=Harness.CURSOR,
            harness_id="K1",
            workspace="/w/proj",
            origin=Origin.PHONE,
            status=SessionStatus.RUNNING,
        )
    )
    assert await asyncio.wait_for(hook(built, "cursor", "stop", STOP_CURSOR), 2) == {}
    assert rec.of(TurnCompleted) and not rec.of(SessionParked)
    assert (await _session(built, "cursor:K1")).status is SessionStatus.IDLE


async def test_agent_host_hooks_with_their_own_session_id_join_the_phone_session(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    """VS Code's hooks report a session id unrelated to the Agent Host's, so the phone's claim links them."""
    built, rec, _ = core
    await built.registry.create_session(
        SessionRecord(
            session_key="vscode_copilot:A1",
            harness=Harness.VSCODE_COPILOT,
            harness_id="A1",
            workspace="/w/proj",
            origin=Origin.PHONE,
            status=SessionStatus.RUNNING,
        )
    )
    await make_claim(built.registry, "/w/proj", "vscode_copilot:A1")
    start: dict[str, JsonValue] = {"session_id": "H1", "cwd": "/w/proj", "source": "new"}
    assert await hook(built, "copilot", "SessionStart", start) == {}
    reply = await hook(built, "copilot", "PreToolUse", {**LOCAL_PRE, "session_id": "H1"})
    assert reply["stdout"]["permissionDecision"] == "allow"  # type: ignore[index,call-overload]
    (session,) = await built.registry.list_sessions()
    assert session.session_key == "vscode_copilot:A1" and session.harness_id == "A1"
    assert session.extra.get(HOOKED) is True
    assert [e.session_key for e in rec.of(ToolStarted)] == ["vscode_copilot:A1"]
    assert not rec.of(SessionStarted)
    end: dict[str, JsonValue] = {"session_id": "H1", "cwd": "/w/proj", "reason": "exit"}
    assert await hook(built, "copilot", "SessionEnd", end) == {}
    assert (await _session(built, "vscode_copilot:A1")).status is SessionStatus.RUNNING
    assert not rec.of(SessionReleased)


async def test_an_agent_host_claim_is_not_taken_by_a_copilot_cli_hook(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    await make_claim(built.registry, "/w/proj", "vscode_copilot:A1")
    await built.registry.create_session(
        SessionRecord(
            session_key="vscode_copilot:A1",
            harness=Harness.VSCODE_COPILOT,
            harness_id="A1",
            workspace="/w/proj",
            origin=Origin.PHONE,
            status=SessionStatus.RUNNING,
        )
    )
    await hook(built, "copilot", "sessionStart", {"sessionId": "C9", "cwd": "/w/proj"})
    keys = sorted(s.session_key for s in await built.registry.list_sessions())
    assert keys == ["copilot_cli:C9", "vscode_copilot:A1"]


def _copilot_events(folder: Path, sid: str, client: str | None) -> str:
    """A Copilot `events.jsonl` as both runtimes write it; only the Agent Host's names a client."""
    metadata = {"variables": {"gitCommitTrailer": {"clientName": client}}} if client else None
    rows = [
        {"type": "session.start", "data": {"sessionId": sid, "producer": "copilot-agent"}},
        {"type": "user.message", "data": {"content": "Reply with exactly: OK"}},
        {"type": "system.message", "data": {"content": "You are Copilot.", "metadata": metadata}},
        {"type": "assistant.message", "data": {"content": "OK", "toolRequests": []}},
    ]
    file = folder / f"{sid}.jsonl"
    file.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows))
    return str(file)


async def test_an_agent_host_desk_chat_takes_its_label_from_its_transcript_at_its_first_stop(
    core: tuple[Core, Recorder, list[str]], tmp_path: Path
) -> None:
    """Scenario 8 on VS Code: its hooks match the Copilot CLI's, and the thread root said `copilot_cli`."""
    built, rec, _ = core
    await away.set_away(built.registry, True)
    await hook(built, "copilot", "SessionStart", {"session_id": "H7", "cwd": "/w/proj", "source": "new"})
    key = "copilot_cli:H7"
    assert (await _session(built, key)).harness is Harness.COPILOT_CLI
    events = _copilot_events(tmp_path, "H7", "vscode-agent-host")
    task = asyncio.create_task(
        hook(built, "copilot", "Stop", {**STOP_LOCAL, "session_id": "H7", "transcript_path": events})
    )
    await _wait_status(built, key, SessionStatus.PARKED)
    await built.service.back(key)
    assert await asyncio.wait_for(task, 2) == {}
    assert (await _session(built, key)).harness is Harness.VSCODE_COPILOT
    (done,) = rec.of(TurnCompleted)
    assert done.harness is Harness.VSCODE_COPILOT and done.text == "OK"


async def test_a_parked_agent_host_desk_chat_is_asked_to_echo_the_phone_line(
    core: tuple[Core, Recorder, list[str]], tmp_path: Path
) -> None:
    """Live 2026-11-21 22:50: the Agent Host's `Stop` was snake case, so no echo was asked for (D141)."""
    built, _, _ = core
    await away.set_away(built.registry, True)
    await hook(built, "copilot", "SessionStart", {"session_id": "H8", "cwd": "/w/proj", "source": "new"})
    stop: dict[str, JsonValue] = {
        **STOP_LOCAL,
        "session_id": "H8",
        "stop_reason": "end_turn",
        "transcript_path": _copilot_events(tmp_path, "H8", "vscode-agent-host"),
    }
    task = asyncio.create_task(hook(built, "copilot", "Stop", stop))
    await _wait_status(built, "copilot_cli:H8", SessionStatus.PARKED)
    await built.service.prompt("copilot_cli:H8", "Reply with exactly: BACK")
    stdout = (await asyncio.wait_for(task, 2))["stdout"]
    assert isinstance(stdout, dict)
    assert stdout["reason"] == (
        f"{PHONE_PREFIX}Reply with exactly: BACK\n\n{ECHO_INSTRUCTION}\n📱 Reply with exactly: BACK"
    )


async def test_a_parked_vscode_chat_shows_the_phone_line_itself_and_is_not_asked_to_echo(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    await hook(built, "copilot", "PreToolUse", LOCAL_PRE)
    task = asyncio.create_task(hook(built, "copilot", "Stop", STOP_LOCAL))
    await _wait_status(built, "vscode:L1", SessionStatus.PARKED)
    await built.service.prompt("vscode:L1", "ship it")
    stdout = (await asyncio.wait_for(task, 2))["stdout"]
    assert isinstance(stdout, dict)
    assert stdout["reason"] == f"{PHONE_PREFIX}ship it" and stdout["systemMessage"] == "📱 ship it"


async def test_a_copilot_cli_session_keeps_its_label_at_a_stop(
    core: tuple[Core, Recorder, list[str]], tmp_path: Path
) -> None:
    built, rec, _ = core
    key = await _running(built, Harness.COPILOT_CLI, "C2", Origin.PHONE, hooked=True)
    stop = {**STOP_CLI, "transcriptPath": _copilot_events(tmp_path, "C2", None)}
    assert await asyncio.wait_for(hook(built, "copilot", "agentStop", stop), 2) == {}
    assert (await _session(built, key)).harness is Harness.COPILOT_CLI
    assert [e.harness for e in rec.of(TurnCompleted)] == [Harness.COPILOT_CLI]


async def _cdp_chat(built: Core) -> None:
    await built.registry.create_session(
        SessionRecord(
            session_key="cursor_cdp:phone-1",
            harness=Harness.CURSOR_CDP,
            harness_id="phone-1",
            workspace="/w/proj",
            origin=Origin.PHONE,
            status=SessionStatus.RUNNING,
        )
    )
    await make_claim(built.registry, "/w/proj", "cursor_cdp:phone-1", scope=CURSOR_SCOPE)


async def test_cursor_hooks_of_a_chat_the_phone_opened_join_it_and_register_no_second_session(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    """Cursor hooks also fire for a chat the CDP adapter drives; without the claim a desk session appeared."""
    built, rec, _ = core
    await away.set_away(built.registry, True)
    await _cdp_chat(built)
    assert await hook(built, "cursor", "stop", {**STOP_CURSOR, "conversation_id": "K9"}) == {}
    assert await hook(built, "cursor", "stop", {**STOP_CURSOR, "conversation_id": "K9"}) == {}
    keys = [s.session_key for s in await built.registry.list_sessions()]
    assert keys == ["cursor_cdp:phone-1"]
    assert not rec.of(SessionStarted) and not rec.of(SessionParked)


async def test_a_cursor_hook_that_beats_the_cdp_session_record_still_joins_it(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    """The chat's first hook fires as it opens, before the adapter has stored the session."""
    built, rec, _ = core
    await away.set_away(built.registry, True)
    await make_claim(built.registry, "/w/proj", "cursor_cdp:phone-1", scope=CURSOR_SCOPE)

    async def store_later() -> None:
        await asyncio.sleep(0.3)
        await _cdp_chat(built)

    later = asyncio.create_task(store_later())
    start: dict[str, JsonValue] = {"conversation_id": "K9", "workspace_roots": ["/w/proj"]}
    assert await hook(built, "cursor", "sessionStart", start) == {}
    await later
    assert [s.session_key for s in await built.registry.list_sessions()] == ["cursor_cdp:phone-1"]
    assert not rec.of(SessionStarted)


async def test_a_cdp_claim_is_not_taken_by_a_vscode_hook(core: tuple[Core, Recorder, list[str]]) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    await _cdp_chat(built)
    start: dict[str, JsonValue] = {"session_id": "H1", "cwd": "/w/proj", "source": "new"}
    await hook(built, "copilot", "SessionStart", start)
    assert await take_claim(built.registry, "/w/proj", scope=CURSOR_SCOPE) is not None


async def test_a_hooked_session_is_marked_so_the_agent_host_adapter_leaves_its_tool_events_here(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    await hook(built, "copilot", "PreToolUse", LOCAL_PRE)
    (session,) = await built.registry.list_sessions()
    assert session.extra.get(HOOKED) is True


STOP_CLI: dict[str, JsonValue] = {"sessionId": "C2", "cwd": "/w/proj", "stopReason": "end_turn"}
STOP_AHP: dict[str, JsonValue] = {**STOP_LOCAL, "session_id": "A1"}
STOP_TARGETS = [
    ("copilot", "Stop", STOP_LOCAL, Harness.VSCODE, "L1"),
    ("copilot", "Stop", STOP_AHP, Harness.VSCODE_COPILOT, "A1"),
    ("copilot", "agentStop", STOP_CLI, Harness.COPILOT_CLI, "C2"),
    ("cursor", "stop", STOP_CURSOR, Harness.CURSOR, "K1"),
]


async def _running(built: Core, harness: Harness, sid: str, origin: Origin, hooked: bool) -> str:
    record = SessionRecord(
        session_key=f"{harness.value}:{sid}",
        harness=harness,
        harness_id=sid,
        workspace="/w/proj",
        origin=origin,
        status=SessionStatus.RUNNING,
        extra={HOOKED: True} if hooked else {},
    )
    await built.registry.create_session(record)
    return record.session_key


def _continuation_text(reply: dict[str, JsonValue]) -> str:
    return str(reply["stdout"])


@pytest.mark.parametrize(("family", "event", "payload", "harness", "sid"), STOP_TARGETS)
async def test_a_phone_session_with_away_off_gets_its_queued_message_when_its_turn_ends(
    core: tuple[Core, Recorder, list[str]],
    family: str,
    event: str,
    payload: dict[str, JsonValue],
    harness: Harness,
    sid: str,
) -> None:
    built, rec, _ = core
    key = await _running(built, harness, sid, Origin.PHONE, hooked=True)
    assert await built.service.prompt(key, "also add tests") == "Queued (1 waiting)"
    reply = await asyncio.wait_for(hook(built, family, event, payload), 2)
    assert "also add tests" in _continuation_text(reply)
    assert await built.service.queued(key) == []
    assert (await _session(built, key)).status is SessionStatus.RUNNING
    assert not rec.of(SessionParked)
    assert await asyncio.wait_for(hook(built, family, event, payload), 2) == {}


async def test_a_hooked_desk_session_gets_its_queued_message_after_away_goes_off(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    key = await _running(built, Harness.VSCODE, "L1", Origin.DESK, hooked=True)
    await built.service.prompt(key, "ship it")
    reply = await hook(built, "copilot", "Stop", STOP_LOCAL)
    assert "ship it" in _continuation_text(reply)
    assert await built.service.queued(key) == []
    assert rec.events == []


async def test_a_desk_session_the_relay_never_hooked_stays_untouched_with_away_off(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    key = await _running(built, Harness.VSCODE, "L1", Origin.DESK, hooked=False)
    await built.registry.enqueue(key, "prompt", "hello")
    assert await hook(built, "copilot", "Stop", STOP_LOCAL) == {}
    assert [q.text for q in await built.service.queued(key)] == ["hello"]
    assert rec.events == []


async def test_queued_messages_go_out_once_with_away_on(core: tuple[Core, Recorder, list[str]]) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    key = await _running(built, Harness.VSCODE, "L1", Origin.PHONE, hooked=True)
    await built.service.prompt(key, "first")
    await built.service.prompt(key, "second")
    reply = await asyncio.wait_for(hook(built, "copilot", "Stop", STOP_LOCAL), 2)
    text = _continuation_text(reply)
    assert "first" in text and "second" in text
    assert await built.service.queued(key) == []
    assert len(rec.of(TurnCompleted)) == 1


async def test_a_message_queued_while_another_stop_hook_is_continuing_is_still_delivered(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, _, _ = core
    await away.set_away(built.registry, True)
    key = await _running(built, Harness.VSCODE, "L1", Origin.PHONE, hooked=True)
    await built.registry.enqueue(key, "prompt", "queued while the other hook runs")
    reply = await asyncio.wait_for(
        hook(built, "copilot", "Stop", {**STOP_LOCAL, "stop_hook_active": True}), 2
    )
    assert "queued while the other hook runs" in _continuation_text(reply)
    assert await built.service.queued(key) == []


async def test_nothing_is_delivered_at_a_stop_after_a_panic(core: tuple[Core, Recorder, list[str]]) -> None:
    built, _, _ = core
    key = await _running(built, Harness.VSCODE, "L1", Origin.PHONE, hooked=True)
    await built.registry.enqueue(key, "prompt", "too late")
    paths.pause_flag().parent.mkdir(parents=True, exist_ok=True)
    paths.pause_flag().write_text("now", encoding="utf-8")
    assert await hook(built, "copilot", "Stop", STOP_LOCAL) == {}
    assert [q.text for q in await built.service.queued(key)] == ["too late"]


@pytest.mark.parametrize("away_on", [False, True])
async def test_queue_mode_holds_a_message_for_the_turn_end_not_the_next_tool_call(
    core: tuple[Core, Recorder, list[str]], away_on: bool
) -> None:
    built, _, _ = core
    await away.set_away(built.registry, away_on)
    key = await _running(built, Harness.VSCODE, "L1", Origin.PHONE, hooked=True)
    assert await built.service.queue_mode(key) == "queue"
    await built.service.prompt(key, "after this turn")
    post = {**LOCAL_PRE, "tool_name": "read_file", "tool_response": "ok"}
    assert await hook(built, "copilot", "PostToolUse", post) == {}
    assert [q.kind for q in await built.service.queued(key)] == ["prompt"]


async def test_a_steer_item_rides_the_tool_call_while_a_queued_prompt_waits(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, _, _ = core
    key = await _running(built, Harness.VSCODE, "L1", Origin.PHONE, hooked=True)
    await built.registry.enqueue(key, "prompt", "later")
    await built.registry.enqueue(key, "steer", "now")
    post = {**LOCAL_PRE, "tool_name": "read_file", "tool_response": "ok"}
    reply = await hook(built, "copilot", "PostToolUse", post)
    text = _continuation_text(reply)
    assert "now" in text and "later" not in text
    assert [q.text for q in await built.service.queued(key)] == ["later"]
