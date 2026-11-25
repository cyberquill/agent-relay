"""D146: taking over a desk chat: drive it, arm it, or hand it off; never lie about which."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from agent_relay.adapters.base import AdapterUnavailableError, StartRequest
from agent_relay.core import away
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayError, RelayService
from agent_relay.core.service.takeover import HANDOFF_OF, TAKE_MODE
from agent_relay.core.signals import Signals
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.config import RelayConfig, Workspace, WorkspacesConfig
from agent_relay.models.events import Event, Harness, Origin, SessionStarted
from agent_relay.models.sessions import SessionRecord, SessionStatus
from agent_relay.settings import save_workspaces
from tests.core import chat_stores as stores
from tests.fakes.adapters import EmittingAdapter

REFUSAL = "VS Code's Agent Host does not list that chat (is its window open?)"


class Driven(EmittingAdapter):
    """OpenCode-like: can adopt a chat by id."""

    capabilities = Capabilities(cold_start=True, adopt=True, cancel=True)

    def __init__(self, registry: Registry) -> None:
        super().__init__(count=0)
        self._registry = registry
        self.adopted: list[tuple[str, str, str | None]] = []

    async def adopt(self, native_id: str, workspace: str, title: str | None) -> SessionRecord:
        self.adopted.append((native_id, workspace, title))
        return await self._registry.create_session(
            SessionRecord(
                session_key=f"{self.harness.value}:{native_id}",
                harness=self.harness,
                harness_id=native_id,
                workspace=workspace,
                title=title,
                origin=Origin.DESK,
                status=SessionStatus.IDLE,
            )
        )


class HostAdapter(Driven):
    """VS Code Copilot's Agent Host, which can refuse to list a chat."""

    harness = Harness.VSCODE_COPILOT

    def __init__(self, registry: Registry, *, lists: bool = True) -> None:
        super().__init__(registry)
        self.lists = lists

    async def adopt(self, native_id: str, workspace: str, title: str | None) -> SessionRecord:
        if not self.lists:
            raise AdapterUnavailableError(REFUSAL)
        return await super().adopt(native_id, workspace, title)

    async def start_session(self, request: StartRequest, attachments: Sequence[object] = ()) -> SessionRecord:
        record = await super().start_session(request)
        return record.model_copy(
            update={"harness": self.harness, "session_key": f"{self.harness.value}:h{len(self.started)}"}
        )


class CursorCli(EmittingAdapter):
    harness = Harness.CURSOR_CLI

    async def start_session(self, request: StartRequest, attachments: Sequence[object] = ()) -> SessionRecord:
        record = await super().start_session(request)
        return record.model_copy(
            update={"harness": self.harness, "session_key": f"{self.harness.value}:n{len(self.started)}"}
        )


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)


class World:
    def __init__(
        self, service: RelayService, router: Router, registry: Registry, proj: Path, recorder: Recorder
    ) -> None:
        self.service = service
        self.router = router
        self.registry = registry
        self.proj = proj.resolve()
        self.recorder = recorder
        self.opencode = Driven(registry)
        self.host = HostAdapter(registry)
        self.cursor_cli = CursorCli(count=0)


@pytest.fixture
async def world(registry: Registry, tmp_path: Path) -> AsyncIterator[World]:
    proj = tmp_path / "proj"
    proj.mkdir()
    save_workspaces(WorkspacesConfig(workspaces=[Workspace(name="proj", path=str(proj))]))
    router = Router()
    recorder = Recorder()
    router.subscribe(recorder)
    service = RelayService(registry, router, Signals(), RelayConfig())
    built = World(service, router, registry, proj, recorder)
    for adapter in (built.opencode, built.host, built.cursor_cli):
        router.add_adapter(adapter)
    yield built
    await router.stop()


async def refs(world: World) -> dict[str, str]:
    return {c.native_id: c.ref for c in (await world.service.chats()).chats}


async def test_a_finished_opencode_chat_is_driven_in_place(world: World) -> None:
    stores.opencode_db(world.proj, "ses_1")
    taken = await world.service.take_chat((await refs(world))["ses_1"])
    assert taken.mode == "drive"
    assert world.opencode.adopted == [("ses_1", str(world.proj), "Send the report")]
    session = taken.session
    assert session.origin is Origin.DESK and session.extra[TAKE_MODE] == "drive"
    assert away.taken_over(session) and away.phone_held(session) and not away.armed(session)
    assert "Reply here to send this chat its next turn" in taken.recap
    assert "Send the report" in taken.recap and "Sent." in taken.recap


async def test_taking_over_announces_the_session_so_the_phone_gets_a_thread(world: World) -> None:
    stores.opencode_db(world.proj, "ses_1")
    await world.service.take_chat((await refs(world))["ses_1"])
    await world.router.join()
    started = [e for e in world.recorder.events if isinstance(e, SessionStarted)]
    assert [e.session_key for e in started] == ["opencode:ses_1"]


async def test_taking_the_same_chat_twice_reuses_its_session(world: World) -> None:
    stores.opencode_db(world.proj, "ses_1")
    ref = (await refs(world))["ses_1"]
    first = await world.service.take_chat(ref)
    second = await world.service.take_chat(ref)
    assert first.session.session_key == second.session.session_key
    assert len(world.opencode.adopted) == 1


async def test_a_chat_the_app_refuses_to_list_falls_back_to_a_handoff_that_says_why(world: World) -> None:
    world.host.lists = False
    stores.copilot_chat(world.proj, "h1", host=True)
    taken = await world.service.take_chat((await refs(world))["h1"])
    assert taken.mode == "handoff"
    assert "does not list that chat" in taken.recap and "**new** session" in taken.recap
    assert (taken.session.title or "").startswith("↪ ")
    assert taken.session.extra[HANDOFF_OF] == "vscode_copilot:h1"
    assert taken.session.origin is Origin.PHONE and not away.taken_over(taken.session)


async def test_a_handoff_prompt_carries_the_transcript_and_the_last_exchange(world: World) -> None:
    world.host.lists = False
    stores.copilot_chat(world.proj, "h1", host=True, prompt="Run the triage skill", reply="Triage is done.")
    await world.service.take_chat((await refs(world))["h1"])
    prompt = world.host.started[0].prompt
    assert "events.jsonl" in prompt and "never modify" in prompt
    assert "Run the triage skill" in prompt and "Triage is done." in prompt
    assert "Change nothing yet" in prompt


async def test_a_copilot_cli_chat_is_continued_as_a_new_vscode_copilot_session(world: World) -> None:
    stores.copilot_chat(world.proj, "k1", host=False)
    taken = await world.service.take_chat((await refs(world))["k1"])
    assert taken.mode == "handoff" and taken.session.harness is Harness.VSCODE_COPILOT
    assert taken.session.extra[HANDOFF_OF] == "copilot_cli:k1"
    assert "Copilot CLI" in taken.recap


async def test_a_cursor_desktop_chat_is_continued_as_a_new_cursor_cli_session(world: World) -> None:
    stores.cursor_chat(world.proj, "c1")
    taken = await world.service.take_chat((await refs(world))["c1"])
    assert taken.mode == "handoff" and taken.session.harness is Harness.CURSOR_CLI
    assert taken.session.extra[HANDOFF_OF] == "cursor:c1"


async def test_a_running_hooked_chat_is_armed_so_its_next_stop_parks_it(world: World) -> None:
    stores.cursor_chat(world.proj, "c1", finished=False, seconds_old=5)
    taken = await world.service.take_chat((await refs(world))["c1"])
    assert taken.mode == "arm"
    session = taken.session
    assert session.session_key == "cursor:c1" and session.harness is Harness.CURSOR
    assert session.origin is Origin.DESK and session.status is SessionStatus.RUNNING
    assert away.armed(session) and away.taken_over(session)
    assert "holds" in taken.recap


async def test_arming_reuses_the_session_a_hook_already_made(world: World) -> None:
    stores.copilot_chat(world.proj, "L1", host=False, finished=False, seconds_old=5)
    await world.registry.create_session(
        SessionRecord(
            session_key="copilot_cli:L1",
            harness=Harness.COPILOT_CLI,
            harness_id="L1",
            workspace=str(world.proj),
            origin=Origin.DESK,
            status=SessionStatus.RUNNING,
        )
    )
    taken = await world.service.take_chat((await refs(world))["L1"])
    assert taken.session.session_key == "copilot_cli:L1" and away.armed(taken.session)
    assert len(await world.registry.list_sessions()) == 1


async def test_a_running_chat_in_an_app_without_hooks_is_refused(world: World) -> None:
    stores.opencode_db(world.proj, "ses_1", finished=False, seconds_old=3)
    with pytest.raises(RelayError, match="running at the desk now"):
        await world.service.take_chat((await refs(world))["ses_1"])


async def test_a_chat_that_has_left_the_list_is_an_error_not_a_guess(world: World) -> None:
    with pytest.raises(RelayError, match="not in the list any more"):
        await world.service.take_chat("opencode:ghost")


async def test_back_hands_a_taken_over_chat_to_the_desk(world: World) -> None:
    stores.cursor_chat(world.proj, "c1", finished=False, seconds_old=5)
    taken = await world.service.take_chat((await refs(world))["c1"])
    key = taken.session.session_key
    assert await world.service.back(key) == "Handed to the desk."
    session = await world.registry.get_session(key)
    assert session is not None and not away.taken_over(session) and not away.armed(session)


async def test_turning_away_off_hands_every_taken_over_chat_back(world: World) -> None:
    stores.opencode_db(world.proj, "ses_1")
    taken = await world.service.take_chat((await refs(world))["ses_1"])
    reply = await world.service.set_away(False)
    assert "Handed 1 phone session(s) to the desk." in reply
    session = await world.registry.get_session(taken.session.session_key)
    assert session is not None and not away.taken_over(session)


async def test_chats_validates_the_window_and_the_workspace(world: World) -> None:
    with pytest.raises(RelayError, match="1 to 90 days"):
        await world.service.chats(days=0)
    with pytest.raises(RelayError, match="Unknown workspace"):
        await world.service.chats(workspace="nope")
