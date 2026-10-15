"""AC-30 approvals with an explicit deny at the relay's deadline, AC-31 parking, AC-32 loop guards,
plus steering, wake-up and the no-noise-at-the-desk rule."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay import paths
from agent_relay.adapters.base import Decision
from agent_relay.core import away
from agent_relay.core.claims import make_claim
from agent_relay.core.hooks import APPROVED_CALL_KEY, HOOKED
from agent_relay.core.parking import LIMIT_TEXT, RENEW_TEXT
from agent_relay.core.registry import RECENT_APPROVAL_PREFIX, Registry, stamped_at
from agent_relay.core.service.approvals import record_workspace_grant
from agent_relay.daemon import Core
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import (
    ApprovalRequested,
    ApprovalResolved,
    Event,
    Harness,
    Origin,
    SessionParked,
    SessionReleased,
    SessionStarted,
    TurnCompleted,
    utcnow,
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
    assert [s.session_key for s in sessions] == ["vscode:L1"]
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


async def _when_asked(rec: Recorder) -> str:
    for _ in range(200):
        if rec.of(ApprovalRequested):
            return rec.of(ApprovalRequested)[-1].request_id
        await asyncio.sleep(0.005)
    raise AssertionError("no approval requested")


async def test_away_off_hands_a_desk_approval_back_to_the_desk(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    task = asyncio.create_task(hook(built, "cursor", "beforeShellExecution", CURSOR_SHELL))
    request_id = await _when_asked(rec)
    await built.service.set_away(False)
    assert await asyncio.wait_for(task, 1) == {}
    resolved = rec.of(ApprovalResolved)
    assert [(r.request_id, r.decision, r.by) for r in resolved] == [(request_id, "desk", "desk")]
    assert await built.registry.open_pending() == []


async def test_away_off_keeps_a_phone_session_approval_on_the_phone(
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
    task = asyncio.create_task(
        hook(built, "opencode", "tool.execute.before", {**OC_PRE, "sessionID": "ses_p"})
    )
    await _when_asked(rec)
    await built.service.set_away(False)
    assert not rec.of(ApprovalResolved)
    reply = await asyncio.wait_for(task, 2)
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and stdout["decision"] == "deny"
    assert rec.of(ApprovalResolved)[-1].by == "deadline"


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


async def test_away_off_hands_a_vscode_phone_session_and_its_approval_to_the_desk(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    key = await _phone_session(built, Harness.VSCODE, "L1")
    task = asyncio.create_task(hook(built, "copilot", "PreToolUse", LOCAL_PRE))
    request_id = await _when_asked(rec)
    assert "Handed 1 phone session(s) to the desk" in await built.service.set_away(False)
    assert await asyncio.wait_for(task, 1) == {}
    assert [(r.request_id, r.by) for r in rec.of(ApprovalResolved)] == [(request_id, "desk")]
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


async def test_a_workspace_away_off_reaches_any_folder_of_a_multi_root_phone_session(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, _, _ = core
    multi = await _phone_session(
        built, Harness.VSCODE, "M1", workspace="/w/one", extra={away.FOLDERS: ["/w/one", "/w/two"]}
    )
    other = await _phone_session(built, Harness.VSCODE, "O1", workspace="/w/three")
    assert "Handed 1 phone session(s)" in await built.service.set_away_for(False, "/w/two")
    assert not away.phone_held(await _session(built, multi))
    assert away.phone_held(await _session(built, other))


async def _answer_deny_with(built: Core, rec: Recorder, reason: str | None) -> None:
    request_id = await _when_asked(rec)
    await built.service.answer_approval(request_id, "deny", reason=reason)


async def _pre_tool_decision(built: Core, payload: dict[str, JsonValue]) -> JsonValue:
    reply = await hook(built, "copilot", "PreToolUse", payload)
    stdout = reply["stdout"]
    assert isinstance(stdout, dict)
    return stdout["permissionDecision"]


def _message_of(stdout: dict[str, JsonValue]) -> str:
    found = (
        stdout.get("permissionDecisionReason")
        or stdout.get("message")
        or stdout.get("agent_message")
        or stdout.get("reason")
    )
    assert isinstance(found, str)
    return found


@pytest.mark.parametrize(
    ("family", "event", "payload"),
    [
        ("copilot", "PreToolUse", LOCAL_PRE),
        ("copilot", "permissionRequest", CLI_PERM),
        ("copilot", "preToolUse", CLI_PERM),
        ("cursor", "beforeShellExecution", CURSOR_SHELL),
        ("opencode", "tool.execute.before", OC_PRE),
    ],
)
async def test_a_deny_reason_reaches_the_agent_in_every_harness_format(
    core: tuple[Core, Recorder, list[str]], family: str, event: str, payload: dict[str, JsonValue]
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    answering = asyncio.create_task(_answer_deny_with(built, rec, "use the staging db"))
    reply = await hook(built, family, event, payload)
    await answering
    stdout = reply["stdout"]
    assert isinstance(stdout, dict)
    assert _message_of(stdout) == "Denied from the phone by the user: use the staging db"
    rec.events.clear()
    other: dict[str, JsonValue] = {**payload}
    for key in ("tool_input", "toolArgs", "command", "args"):
        if key in other:
            other[key] = "something else" if isinstance(other[key], str) else {"command": "other"}
    answering = asyncio.create_task(_answer_deny_with(built, rec, None))
    reply = await hook(built, family, event, other)
    await answering
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and _message_of(stdout) == "Denied from the phone by the user."


async def test_a_workspace_grant_skips_later_prompts_for_that_tool_in_that_workspace(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    answering = asyncio.create_task(_answer_when_asked(built, rec, "allow_workspace"))
    assert await _pre_tool_decision(built, LOCAL_PRE) == "allow"
    await answering
    rec.events.clear()
    again: dict[str, JsonValue] = {**LOCAL_PRE, "tool_input": {"command": "ls -la"}, "tool_use_id": "t2"}
    assert await _pre_tool_decision(built, again) == "allow"
    nested: dict[str, JsonValue] = {
        **again,
        "session_id": "L2",
        "cwd": "/w/proj/src/deep",
        "tool_use_id": "t3",
    }
    assert await _pre_tool_decision(built, nested) == "allow"
    assert not rec.of(ApprovalRequested)
    assert await built.registry.kv_get(APPROVED_CALL_KEY.format(call_id="t2")) == "allow"


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"session_id": "L3", "cwd": "/w/other"}, "another workspace"),
        ({"session_id": "L4", "cwd": "/w"}, "the parent folder"),
        ({"tool_name": "edit_file"}, "another tool"),
    ],
)
async def test_a_workspace_grant_does_not_leak_to_another_workspace_or_tool(
    core: tuple[Core, Recorder, list[str]], overrides: dict[str, JsonValue], why: str
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    answering = asyncio.create_task(_answer_when_asked(built, rec, "allow_workspace"))
    await _pre_tool_decision(built, LOCAL_PRE)
    await answering
    rec.events.clear()
    assert await _pre_tool_decision(built, {**LOCAL_PRE, **overrides, "tool_use_id": "t9"}) == "deny", why
    assert rec.of(ApprovalRequested), why


async def test_an_expired_workspace_grant_asks_again(core: tuple[Core, Recorder, list[str]]) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    gone = utcnow() - timedelta(hours=1)
    await record_workspace_grant(built.registry, "/w/proj", "run_in_terminal", 60, gone)
    assert await _pre_tool_decision(built, LOCAL_PRE) == "deny"
    assert rec.of(ApprovalRequested)


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


_PRE_C9: dict[str, JsonValue] = {
    "session_id": "C9",
    "cwd": "/w/proj",
    "tool_name": "bash",
    "tool_input": {"command": "make"},
}
_PERM_C9: dict[str, JsonValue] = {**_PRE_C9, "toolName": "bash"}


async def _approve_then_stamp(built: Core, rec: Recorder, stamp: str | None) -> str:
    """Approve one call from the phone, then overwrite the stored recent-approval value."""
    answering = asyncio.create_task(_answer_when_asked(built, rec, "allow"))
    await hook(built, "copilot", "PreToolUse", _PRE_C9)
    await answering
    rec.events.clear()
    (key,) = await built.registry.kv_prefix(RECENT_APPROVAL_PREFIX)
    if stamp is not None:
        await built.registry.kv_set(key, stamp)
    return key


async def test_recent_approval_is_scoped_to_the_session_and_stamped(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    key = await _approve_then_stamp(built, rec, None)
    assert key.startswith(f"{RECENT_APPROVAL_PREFIX}copilot_cli:C9:")
    stamped = stamped_at((await built.registry.kv_get(key)) or "")
    assert stamped is not None and utcnow() - stamped < timedelta(seconds=5)


@pytest.mark.parametrize("age", [timedelta(seconds=61), timedelta(hours=3)])
async def test_recent_approval_expires_after_sixty_seconds(
    core: tuple[Core, Recorder, list[str]], age: timedelta
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    await _approve_then_stamp(built, rec, f"{(utcnow() - age).isoformat()} allow")
    reply = await hook(built, "copilot", "permissionRequest", _PERM_C9)
    assert rec.of(ApprovalRequested), "an old approval must not be reused"
    assert reply["stdout"]["behavior"] == "deny"  # type: ignore[index,call-overload]


async def test_recent_approval_without_a_stamp_is_not_honoured(
    core: tuple[Core, Recorder, list[str]],
) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    await _approve_then_stamp(built, rec, "allow")
    await hook(built, "copilot", "permissionRequest", _PERM_C9)
    assert rec.of(ApprovalRequested)


async def test_recent_denial_is_never_reused(core: tuple[Core, Recorder, list[str]]) -> None:
    built, rec, _ = core
    await away.set_away(built.registry, True)
    await _approve_then_stamp(built, rec, f"{utcnow().isoformat()} deny")
    await hook(built, "copilot", "permissionRequest", _PERM_C9)
    assert rec.of(ApprovalRequested)


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
    assert blocked == cap
    assert notes == [LIMIT_TEXT]
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
            "additionalContext": "Message from the user, sent from their phone:\n\nuse pytest, not unittest",
        }
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
    await built.registry.create_session(
        SessionRecord(
            session_key="cursor:Z", harness=Harness.CURSOR, harness_id="Z", status=SessionStatus.PARKED
        )
    )
    assert built.parking is not None
    assert await built.parking.recover() == 1
    assert (await _session(built, "cursor:Z")).status is SessionStatus.IDLE


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


async def test_phone_sessions_are_gated_even_with_away_off(core: tuple[Core, Recorder, list[str]]) -> None:
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
    payload: dict[str, JsonValue] = {**OC_PRE, "sessionID": "ses_p"}
    answering = asyncio.create_task(_answer_when_asked(built, rec, "deny"))
    reply = await hook(built, "opencode", "tool.execute.before", payload)
    await answering
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and stdout["decision"] == "deny"
    assert await built.registry.kv_get("approved_call:call_1") == "deny"


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
    answering = asyncio.create_task(_answer_when_asked(built, rec, "allow"))
    reply = await hook(built, "copilot", "PreToolUse", {**LOCAL_PRE, "session_id": "H1"})
    await answering
    assert reply["stdout"]["permissionDecision"] == "allow"  # type: ignore[index,call-overload]
    (session,) = await built.registry.list_sessions()
    assert session.session_key == "vscode_copilot:A1" and session.harness_id == "A1"
    assert session.extra.get(HOOKED) is True
    assert [e.session_key for e in rec.of(ApprovalRequested)] == ["vscode_copilot:A1"]
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


async def test_hooked_marker_and_copilot_call_decisions(core: tuple[Core, Recorder, list[str]]) -> None:
    """The AHP adapter relies on both: hooked sessions keep tool events here, and a phone decision for
    a tool call is reused when the Agent Host asks to confirm the same call."""
    built, rec, _ = core
    await away.set_away(built.registry, True)
    answering = asyncio.create_task(_answer_when_asked(built, rec, "allow"))
    await hook(built, "copilot", "PreToolUse", LOCAL_PRE)
    await answering
    (session,) = await built.registry.list_sessions()
    assert session.extra.get(HOOKED) is True
    assert await built.registry.kv_get(APPROVED_CALL_KEY.format(call_id="t1")) == "allow"


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
