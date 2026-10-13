"""AC-18: the vscode adapter against a fake relay-bridge window on a real relay.sock."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay import paths
from agent_relay.adapters.base import AdapterUnavailableError, UnsupportedError
from agent_relay.adapters.vscode.adapter import VIA_CLI, VIA_KEY, WINDOW_KEY, VsCodeAdapter
from agent_relay.adapters.vscode.hub import BridgeCommandError, BridgeGoneError, BridgeHub
from agent_relay.core.claims import UNCLAIMED_PREFIX, claim_key
from agent_relay.core.registry import Registry
from agent_relay.daemon import Core
from agent_relay.ipc import IpcServer
from agent_relay.models.capabilities import Capability
from agent_relay.models.config import RelayConfig, Workspace, WorkspacesConfig
from agent_relay.models.events import ErrorEvent, Event, Harness, Origin, SessionStarted
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import save_workspaces
from tests.fakes.bridge import FakeBridge

WORKSPACE = "/w/proj"


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


@dataclass
class FakeLauncher:
    calls: list[tuple[list[str], Path]] = field(default_factory=list)
    exit_code: int = 0
    on_open: Callable[[], Awaitable[None]] | None = None

    async def __call__(self, argv: Sequence[str], cwd: Path) -> int:
        self.calls.append((list(argv), cwd))
        if self.on_open is not None and "chat" not in argv:
            await self.on_open()
        return self.exit_code


@dataclass
class Rig:
    core: Core
    hub: BridgeHub
    adapter: VsCodeAdapter
    launcher: FakeLauncher
    rec: Recorder
    registry: Registry
    cli: Path
    bridges: list[FakeBridge] = field(default_factory=list)

    async def bridge(self, window_id: str = "win-1", folders: tuple[str, ...] = (WORKSPACE,)) -> FakeBridge:
        bridge = await FakeBridge(paths.socket_path(), window_id, folders).connect()
        self.bridges.append(bridge)
        await until(lambda: window_id in self.hub.windows)
        return bridge

    async def start(self, prompt: str = "fix the failing test", model: str | None = None) -> SessionRecord:
        return await self.core.service.start("vscode", "proj", prompt, model)

    async def session(self, key: str) -> SessionRecord:
        found = await self.registry.get_session(key)
        assert found is not None
        return found

    async def hook(self, event: str, payload: dict[str, JsonValue]) -> dict[str, object]:
        reply = await self.core.handle_ipc(
            {"op": "hook", "harness": "copilot", "event": event, "payload": payload}
        )
        await self.core.router.join()
        return reply


@pytest.fixture
async def rig(registry: Registry, relay_home: Path) -> AsyncIterator[Rig]:
    save_workspaces(WorkspacesConfig(workspaces=[Workspace(name="proj", path=WORKSPACE)]))
    cli = relay_home / "code"
    cli.write_text("")
    core = Core(RelayConfig(), registry)
    rec = Recorder()
    core.router.subscribe(rec)
    hub = BridgeHub(call_timeout=0.5)
    launcher = FakeLauncher()
    adapter = VsCodeAdapter(registry, hub, launcher=launcher, cli=cli, open_timeout=0.3)
    core.router.add_adapter(adapter)
    server = IpcServer(paths.socket_path(), core.handle_ipc, streams=hub.streams())
    await server.start()
    built = Rig(core, hub, adapter, launcher, rec, registry, cli)
    yield built
    for bridge in built.bridges:
        await bridge.close()
    await server.stop()
    await core.router.stop()


# registration


async def test_bridge_window_registers(rig: Rig) -> None:
    bridge = await rig.bridge()
    assert bridge.registered == {"ok": True, "protocol": 1}
    window = rig.hub.windows["win-1"]
    assert window.folders == (Path(WORKSPACE).resolve(),)
    assert window.vscode_version == "1.140.0"
    ok, detail = await rig.adapter.health()
    assert ok and "1 VS Code window" in detail


async def test_register_without_window_id_is_refused(rig: Rig) -> None:
    bridge = FakeBridge(paths.socket_path())
    await bridge.connect({"op": "bridge.register", "folders": [WORKSPACE]})
    rig.bridges.append(bridge)
    assert bridge.registered == {"ok": False, "error": "bridge.register needs a window_id"}
    assert rig.hub.windows == {}


async def test_health_without_a_window_reports_the_fallback(rig: Rig) -> None:
    ok, detail = await rig.adapter.health()
    assert ok and "Falling back to `code chat`" in detail
    rig.cli.unlink()
    ok, detail = await rig.adapter.health()
    assert not ok and "CLI is missing" in detail


async def test_reregistering_a_window_replaces_the_old_connection(rig: Rig) -> None:
    first = await rig.bridge()
    second = await FakeBridge(paths.socket_path(), "win-1", ("/w/other",)).connect()
    rig.bridges.append(second)
    await until(lambda: rig.hub.windows["win-1"].folders == (Path("/w/other").resolve(),))
    assert first.registered == second.registered
    assert len(rig.hub.windows) == 1


async def test_window_choice_prefers_the_deepest_folder(rig: Rig) -> None:
    await rig.bridge("outer", ("/w",))
    await rig.bridge("inner", (WORKSPACE,))
    chosen = rig.hub.window_for(f"{WORKSPACE}/sub")
    assert chosen is not None and chosen.window_id == "inner"
    elsewhere = rig.hub.window_for("/w/else")
    assert elsewhere is not None and elsewhere.window_id == "outer"
    assert rig.hub.window_for("/nowhere") is None


async def test_folder_changes_update_the_window(rig: Rig) -> None:
    bridge = await rig.bridge()
    await bridge.push({"event": "folders", "folders": [WORKSPACE, "/w/second"]})
    await until(lambda: rig.hub.window_for("/w/second") is not None)


async def test_disconnect_removes_the_window(rig: Rig) -> None:
    bridge = await rig.bridge()
    await bridge.close()
    await until(lambda: rig.hub.windows == {})


# starting chats


async def test_start_opens_a_local_chat_through_the_bridge(rig: Rig) -> None:
    bridge = await rig.bridge()
    record = await rig.start(model="gpt-5")
    assert bridge.args_of("start") == {"query": "fix the failing test", "model": "gpt-5"}
    assert rig.launcher.calls == []
    assert record.harness is Harness.VSCODE
    assert record.origin is Origin.PHONE
    assert record.status is SessionStatus.RUNNING
    assert record.harness_id.startswith(UNCLAIMED_PREFIX)
    assert record.extra[WINDOW_KEY] == "win-1"
    assert record.model == "gpt-5"
    assert await rig.registry.kv_get(claim_key(WORKSPACE)) is not None
    assert [s.origin for s in rig.rec.of(SessionStarted)] == [Origin.PHONE]


async def test_first_local_hook_claims_the_phone_chat(rig: Rig) -> None:
    await rig.bridge()
    record = await rig.start()
    await rig.hook(
        "UserPromptSubmit", {"session_id": "L9", "cwd": WORKSPACE, "prompt": "fix the failing test"}
    )
    linked = await rig.session(record.session_key)
    assert linked.harness_id == "L9"
    assert await rig.registry.kv_get(claim_key(WORKSPACE)) is None
    found = await rig.registry.find_session(Harness.VSCODE, "L9")
    assert found is not None and found.session_key == record.session_key
    await rig.hook("UserPromptSubmit", {"session_id": "L10", "cwd": WORKSPACE, "prompt": "desk chat"})
    assert await rig.registry.find_session(Harness.COPILOT_CLI, "L10") is None
    assert len(rig.rec.of(SessionStarted)) == 1


async def test_camel_case_hooks_never_claim(rig: Rig) -> None:
    await rig.bridge()
    record = await rig.start()
    await rig.hook("userPromptSubmitted", {"sessionId": "C1", "cwd": WORKSPACE, "prompt": "x"})
    assert (await rig.session(record.session_key)).harness_id.startswith(UNCLAIMED_PREFIX)
    assert await rig.registry.kv_get(claim_key(WORKSPACE)) is not None


async def test_start_opens_the_workspace_when_no_window_holds_it(rig: Rig) -> None:
    async def open_window() -> None:
        await rig.bridge()

    rig.launcher.on_open = open_window
    record = await rig.start()
    assert rig.launcher.calls == [([str(rig.cli), WORKSPACE], Path(WORKSPACE))]
    assert rig.bridges[0].commands() == ["start"]
    assert record.extra[WINDOW_KEY] == "win-1"


async def test_start_falls_back_to_code_chat_without_the_extension(rig: Rig) -> None:
    record = await rig.start(model="gpt-5")
    assert rig.launcher.calls == [
        ([str(rig.cli), WORKSPACE], Path(WORKSPACE)),
        ([str(rig.cli), "chat", "-m", "agent", "-r", "fix the failing test"], Path(WORKSPACE)),
    ]
    assert record.extra[VIA_KEY] == VIA_CLI
    assert WINDOW_KEY not in record.extra


MULTI = (WORKSPACE, "/w/two")


def _multi_root(tmp_path: Path) -> Path:
    file = tmp_path / "team.code-workspace"
    file.write_text('{"folders": [{"path": "/w/proj"}, {"path": "/w/two"}], // comment\n}')
    save_workspaces(WorkspacesConfig(workspaces=[Workspace(name="proj", path=WORKSPACE, file=str(file))]))
    return file


async def test_multi_root_start_picks_the_window_holding_every_folder(rig: Rig, tmp_path: Path) -> None:
    _multi_root(tmp_path)
    single = await rig.bridge("single", (WORKSPACE,))
    multi = await rig.bridge("multi", MULTI)
    await rig.bridge("later-single", (WORKSPACE,))
    record = await rig.start()
    assert record.extra[WINDOW_KEY] == "multi"
    assert multi.commands() == ["start"] and single.commands() == []
    assert rig.launcher.calls == []


async def test_multi_root_start_opens_the_workspace_file(rig: Rig, tmp_path: Path) -> None:
    file = _multi_root(tmp_path)
    await rig.bridge("single", (WORKSPACE,))

    async def open_window() -> None:
        await rig.bridge("multi", MULTI)

    rig.launcher.on_open = open_window
    record = await rig.start()
    assert rig.launcher.calls == [([str(rig.cli), str(file)], Path(WORKSPACE))]
    assert record.extra[WINDOW_KEY] == "multi"


async def test_multi_root_start_with_an_unreadable_file_fails(rig: Rig, tmp_path: Path) -> None:
    _multi_root(tmp_path).unlink()
    with pytest.raises(AdapterUnavailableError, match="cannot read"):
        await rig.start()
    assert rig.launcher.calls == []


async def test_fallback_prompt_starting_with_a_dash_is_not_an_option(rig: Rig) -> None:
    await rig.start("--help me")
    argv, _ = rig.launcher.calls[-1]
    assert argv[-1] == " --help me"


async def test_fallback_reports_a_failing_cli(rig: Rig) -> None:
    rig.launcher.exit_code = 3
    with pytest.raises(AdapterUnavailableError, match="exited with 3"):
        await rig.start()


async def test_fallback_needs_the_cli(rig: Rig) -> None:
    rig.cli.unlink()
    with pytest.raises(AdapterUnavailableError, match="CLI is missing"):
        await rig.start()


async def test_a_failing_start_ends_the_session(rig: Rig) -> None:
    bridge = await rig.bridge()
    bridge.errors["start"] = "no chat view"
    with pytest.raises(BridgeCommandError, match="no chat view"):
        await rig.start()
    (record,) = [
        s for s in await rig.registry.list_sessions(include_ended=True) if s.harness is Harness.VSCODE
    ]
    assert record.status is SessionStatus.ENDED


async def test_an_unanswered_command_times_out(rig: Rig) -> None:
    bridge = await rig.bridge()
    bridge.silent.add("start")
    with pytest.raises(BridgeGoneError, match="did not answer"):
        await rig.start()


# prompts, steering, cancel


async def idle(rig: Rig, record: SessionRecord) -> SessionRecord:
    await rig.registry.set_session_status(record.session_key, SessionStatus.IDLE)
    return await rig.session(record.session_key)


async def test_prompt_to_an_idle_phone_chat_goes_through_the_bridge(rig: Rig) -> None:
    bridge = await rig.bridge()
    record = await idle(rig, await rig.start(model="gpt-5"))
    assert await rig.core.service.prompt(record.session_key, "now add tests") == "sent"
    assert bridge.args_of("prompt") == {"query": "now add tests", "model": "gpt-5"}
    assert (await rig.session(record.session_key)).status is SessionStatus.RUNNING


async def test_prompt_renews_the_claim_until_a_hook_links_the_chat(rig: Rig) -> None:
    await rig.bridge()
    record = await idle(rig, await rig.start())
    await rig.registry.kv_delete(claim_key(WORKSPACE))
    await rig.core.service.prompt(record.session_key, "again")
    assert await rig.registry.kv_get(claim_key(WORKSPACE)) is not None


async def test_prompt_while_running_waits_for_the_next_turn(rig: Rig) -> None:
    bridge = await rig.bridge()
    record = await rig.start()
    receipt = await rig.core.service.prompt(record.session_key, "use pytest")
    assert receipt == "Queued (1 waiting)"
    steered = await rig.core.service.steer(record.session_key, "and ruff")
    assert steered.startswith("Queued (2 waiting). VS Code cannot be steered mid-turn")
    assert "prompt" not in bridge.commands()


async def test_prompt_to_a_chat_whose_window_closed_fails_instead_of_using_another_window(rig: Rig) -> None:
    first = await rig.bridge("win-1")
    record = await idle(rig, await rig.start())
    await first.close()
    await until(lambda: "win-1" not in rig.hub.windows)
    second = await rig.bridge("win-2")
    with pytest.raises(
        AdapterUnavailableError, match="window for this chat is closed. Start a new chat with /new"
    ):
        await rig.core.service.prompt(record.session_key, "continue")
    assert second.commands() == []
    assert (await rig.session(record.session_key)).extra[WINDOW_KEY] == "win-1"


async def test_prompt_to_a_chat_with_no_window_left_does_not_use_code_chat(rig: Rig) -> None:
    record = await idle(rig, await rig.start())
    rig.launcher.calls.clear()
    with pytest.raises(AdapterUnavailableError, match="is closed"):
        await rig.core.service.prompt(record.session_key, "continue")
    assert rig.launcher.calls == []


async def test_waking_a_chat_whose_window_closed_fails(rig: Rig) -> None:
    first = await rig.bridge("win-1")
    record = await idle(rig, await rig.start())
    await first.close()
    await until(lambda: "win-1" not in rig.hub.windows)
    await rig.bridge("win-2")
    with pytest.raises(AdapterUnavailableError, match="is closed"):
        await rig.adapter.wake(record, "wake up")


async def test_desk_local_chats_are_not_prompted_through_the_bridge(rig: Rig) -> None:
    bridge = await rig.bridge()
    desk = await rig.registry.create_session(
        SessionRecord(
            session_key="vscode:D1",
            harness=Harness.VSCODE,
            harness_id="D1",
            workspace=WORKSPACE,
            status=SessionStatus.IDLE,
        )
    )
    receipt = await rig.core.service.prompt(desk.session_key, "hello")
    assert receipt.startswith("queued")
    with pytest.raises(UnsupportedError):
        await rig.adapter.cancel(desk)
    assert bridge.commands() == []


async def test_cancel_runs_the_chat_cancel_command(rig: Rig) -> None:
    bridge = await rig.bridge()
    record = await rig.start()
    await rig.adapter.cancel(record)
    assert bridge.commands() == ["start", "cancel"]


async def test_cancel_without_a_window_is_unavailable(rig: Rig) -> None:
    record = await rig.start()
    with pytest.raises(AdapterUnavailableError):
        await rig.adapter.cancel(record)


async def test_a_disconnect_fails_the_pending_call(rig: Rig) -> None:
    bridge = await rig.bridge()
    record = await rig.start()
    bridge.silent.add("cancel")
    pending = asyncio.create_task(rig.adapter.cancel(record))
    await until(lambda: "cancel" in bridge.commands())
    await bridge.close()
    with pytest.raises(BridgeGoneError, match="disconnected"):
        await pending


# models and approvals


async def test_models_come_from_the_bridge(rig: Rig) -> None:
    await rig.bridge()
    models = await rig.adapter.list_models()
    assert [(m.id, m.label) for m in models] == [("claude-sonnet", "Claude Sonnet"), ("gpt-5", "GPT-5")]


async def test_models_need_a_window(rig: Rig) -> None:
    with pytest.raises(AdapterUnavailableError, match="Open VS Code"):
        await rig.adapter.list_models()


async def test_set_model_applies_to_the_next_prompt(rig: Rig) -> None:
    bridge = await rig.bridge()
    record = await idle(rig, await rig.start())
    await rig.core.service.set_model(record.session_key, "claude-sonnet")
    await rig.core.service.prompt(record.session_key, "go on")
    assert bridge.args_of("prompt")["model"] == "claude-sonnet"


async def test_approvals_are_not_advertised_and_never_reach_the_bridge(rig: Rig) -> None:
    bridge = await rig.bridge()
    record = await rig.start()
    assert not rig.adapter.capabilities.has(Capability.APPROVE)
    for decision in ("allow", "allow_session", "deny"):
        with pytest.raises(UnsupportedError):
            await rig.adapter.answer_approval(record, "r1", decision)
    assert bridge.commands() == ["start"]


# events


async def test_a_failed_chat_turn_reaches_the_phone(rig: Rig) -> None:
    bridge = await rig.bridge()
    record = await rig.start()
    await bridge.push({"event": "error", "message": "model not available"})
    await until(lambda: rig.rec.of(ErrorEvent))
    (error,) = rig.rec.of(ErrorEvent)
    assert error.session_key == record.session_key
    assert "model not available" in error.message
    session = await rig.registry.get_session(record.session_key)
    assert session is not None and session.status is SessionStatus.IDLE, "a running status queues every reply"


async def test_errors_outside_a_phone_chat_are_only_logged(rig: Rig) -> None:
    bridge = await rig.bridge()
    await bridge.push({"event": "error", "message": "nothing open"})
    await bridge.push({"event": "turn.done"})
    await asyncio.sleep(0.05)
    assert rig.rec.of(ErrorEvent) == []
