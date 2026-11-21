"""U2: there are no approvals. Whatever the relay answers, it answers allow, at once, with no card."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core import away
from agent_relay.core.hooks import ALLOW_REASON
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import ApprovalRequested, ApprovalResolved, Event, Harness, Origin
from agent_relay.models.sessions import SessionRecord, SessionStatus
from tests.fakes.adapters import EmittingAdapter


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def of[T](self, kind: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, kind)]


type Rig = tuple[Core, Recorder, EmittingAdapter]


@pytest.fixture
async def rig(registry: Registry) -> AsyncIterator[Rig]:
    core = Core(RelayConfig(), registry)
    rec = Recorder()
    core.router.subscribe(rec)
    adapter = EmittingAdapter(count=0)
    core.router.add_adapter(adapter)
    yield core, rec, adapter
    await core.router.stop()


async def hook(core: Core, family: str, event: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    reply = await core.handle_ipc({"op": "hook", "harness": family, "event": event, "payload": payload})
    await core.router.join()
    return reply  # type: ignore[return-value]


def session(key: str, harness: Harness, origin: Origin) -> SessionRecord:
    return SessionRecord(
        session_key=key,
        harness=harness,
        harness_id=key.split(":", 1)[1],
        workspace="/w/proj",
        origin=origin,
        status=SessionStatus.RUNNING,
    )


VSCODE_PRE: dict[str, JsonValue] = {
    "session_id": "L1",
    "cwd": "/w/proj",
    "tool_name": "run_in_terminal",
    "tool_input": {"command": "rm -rf build"},
    "tool_use_id": "t1",
}
CLI_PERMISSION: dict[str, JsonValue] = {
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
OPENCODE_PRE: dict[str, JsonValue] = {
    "sessionID": "ses_1",
    "callID": "call_1",
    "tool": "bash",
    "args": {"command": "rm x"},
    "directory": "/w/proj",
}


async def test_relay_never_posts_an_approval_card(rig: Rig) -> None:
    core, rec, adapter = rig
    key = "opencode:s1"
    await core.registry.create_session(session(key, Harness.OPENCODE, Origin.PHONE))
    await core.router.publish(
        ApprovalRequested(
            harness=Harness.OPENCODE, session_key=key, request_id="perm_1", tool="bash", detail="rm -rf x"
        )
    )
    await core.router.join()
    assert adapter.approvals == [("perm_1", "allow")]
    assert rec.of(ApprovalRequested) == [] and rec.of(ApprovalResolved) == [], "no sink sees an approval"
    reply = await hook(core, "opencode", "tool.execute.before", {**OPENCODE_PRE, "sessionID": "s1"})
    assert reply["stdout"] == {"decision": "allow", "reason": ALLOW_REASON}
    assert rec.of(ApprovalRequested) == []
    assert await core.registry.open_pending() == [], "no pending row is stored for a tool call"


@pytest.mark.parametrize("harness", [Harness.OPENCODE, Harness.VSCODE_COPILOT, Harness.CURSOR_CLI])
async def test_every_relay_answered_tool_call_is_allowed(harness: Harness, rig: Rig) -> None:
    """Adapter-held: Agent Host ToolAsk, OpenCode permission.asked, Cursor CLI request_permission."""
    core, _, _ = rig
    key = f"{harness.value}:s1"
    adapter = type("HeldAdapter", (EmittingAdapter,), {"harness": harness})(count=0)
    core.router.add_adapter(adapter)
    await core.registry.create_session(session(key, harness, Origin.PHONE))
    await core.router.publish(
        ApprovalRequested(harness=harness, session_key=key, request_id="ask_1", tool="bash", detail="ls")
    )
    await core.router.join()
    assert adapter.approvals == [("ask_1", "allow")]


@pytest.mark.parametrize(
    ("family", "event", "payload", "allowed"),
    [
        ("copilot", "PreToolUse", VSCODE_PRE, {"permissionDecision": "allow"}),
        ("copilot", "preToolUse", CLI_PERMISSION, {"permissionDecision": "allow"}),
        ("copilot", "permissionRequest", CLI_PERMISSION, {"behavior": "allow"}),
        ("cursor", "beforeShellExecution", CURSOR_SHELL, {"permission": "allow"}),
        ("opencode", "tool.execute.before", OPENCODE_PRE, {"decision": "allow"}),
    ],
)
async def test_every_hook_answered_tool_call_is_allowed(
    rig: Rig, family: str, event: str, payload: dict[str, JsonValue], allowed: dict[str, str]
) -> None:
    core, rec, _ = rig
    await away.set_away(core.registry, True)
    reply = await hook(core, family, event, payload)
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and allowed.items() <= stdout.items()
    assert reply["exit"] == 0
    assert rec.of(ApprovalRequested) == [] and await core.registry.open_pending() == []


async def test_a_phone_held_session_is_allowed_with_away_off(rig: Rig) -> None:
    core, rec, _ = rig
    await core.registry.create_session(session("vscode:L1", Harness.VSCODE, Origin.PHONE))
    reply = await hook(core, "copilot", "PreToolUse", VSCODE_PRE)
    stdout = reply["stdout"]
    assert isinstance(stdout, dict) and stdout["permissionDecision"] == "allow"
    assert await core.registry.open_pending() == [] and rec.of(ApprovalRequested) == []


PAYLOADS = [
    ("copilot", "PreToolUse", VSCODE_PRE),
    ("copilot", "permissionRequest", CLI_PERMISSION),
    ("cursor", "beforeShellExecution", CURSOR_SHELL),
    ("opencode", "tool.execute.before", OPENCODE_PRE),
]


@pytest.mark.parametrize(("family", "event", "payload"), PAYLOADS)
async def test_desk_sessions_at_the_desk_are_left_to_the_app(
    rig: Rig, family: str, event: str, payload: dict[str, JsonValue]
) -> None:
    core, rec, _ = rig
    assert await hook(core, family, event, payload) == {}
    assert rec.events == [] and await core.registry.list_sessions() == []


@pytest.mark.parametrize(("family", "event", "payload"), PAYLOADS)
async def test_nothing_is_answered_after_a_panic(
    rig: Rig, family: str, event: str, payload: dict[str, JsonValue]
) -> None:
    core, _, _ = rig
    await away.set_away(core.registry, True)
    paths.pause_flag().parent.mkdir(parents=True, exist_ok=True)
    paths.pause_flag().write_text("paused", encoding="utf-8")
    assert await hook(core, family, event, payload) == {}


async def test_a_paused_relay_leaves_a_phone_held_hook_to_the_app(rig: Rig) -> None:
    core, _, _ = rig
    await core.registry.create_session(session("vscode:L1", Harness.VSCODE, Origin.PHONE))
    paths.pause_flag().parent.mkdir(parents=True, exist_ok=True)
    paths.pause_flag().write_text("paused", encoding="utf-8")
    assert await hook(core, "copilot", "PreToolUse", VSCODE_PRE) == {}
