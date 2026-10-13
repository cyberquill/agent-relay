"""One Webex bot per app: which bot owns a session's thread, what each space lists, and the single-bot
fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import httpx
import pytest
from pydantic import JsonValue

from agent_relay import paths
from agent_relay.adapters.base import StartRequest
from agent_relay.core import away
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayService
from agent_relay.core.signals import Signals
from agent_relay.frontends.webex.access import PERSON_KEY, AccessControl
from agent_relay.frontends.webex.api import WebexApi
from agent_relay.frontends.webex.bot import WebexFrontend
from agent_relay.frontends.webex.bots import BotScope, bot_for_harness, scoped
from agent_relay.frontends.webex.handlers import Handlers
from agent_relay.frontends.webex.renderer import Outbox, WebexRenderer
from agent_relay.frontends.webex.routing import BotRenderers
from agent_relay.models.attachments import Attachment
from agent_relay.models.config import CONTROL, BotName, RelayConfig, Workspace, WorkspacesConfig
from agent_relay.models.events import Harness, Origin, TextDelta
from agent_relay.models.sessions import SessionRecord
from agent_relay.settings import save_workspaces
from tests.fakes.adapters import EmittingAdapter
from tests.fakes.webex import FakeWebex
from tests.frontends.card_tools import choice_values, input_by_id, inputs_of, show_cards
from tests.frontends.conftest import FakeClock


def _input_ids(card: dict[str, JsonValue]) -> set[str]:
    return {str(i["id"]) for i in inputs_of(card)}


class CopilotAdapter(EmittingAdapter):
    harness = Harness.VSCODE_COPILOT

    async def start_session(
        self, request: StartRequest, attachments: Sequence[Attachment] = ()
    ) -> SessionRecord:
        record = await super().start_session(request, attachments)
        return record.model_copy(update={"session_key": f"vscode_copilot:{record.harness_id}"})


def _session(key: str, harness: Harness, thread: str | None = None, bot: str | None = None) -> SessionRecord:
    return SessionRecord(
        session_key=key, harness=harness, harness_id=key, webex_thread_id=thread, webex_bot=bot
    )


# pure routing


def test_each_app_harness_maps_to_its_bot_when_that_bot_is_configured() -> None:
    every: list[BotName] = [CONTROL, "vscode", "cursor", "opencode"]
    for harness in (Harness.VSCODE, Harness.VSCODE_COPILOT, Harness.COPILOT_CLI):
        assert bot_for_harness(harness, every) == "vscode"
    for harness in (Harness.CURSOR, Harness.CURSOR_CLI, Harness.CURSOR_CDP):
        assert bot_for_harness(harness, every) == "cursor"
    assert bot_for_harness(Harness.OPENCODE, every) == "opencode"


def test_an_app_without_its_own_bot_goes_to_control() -> None:
    assert bot_for_harness(Harness.CURSOR, [CONTROL, "vscode"]) == CONTROL
    assert bot_for_harness(Harness.OPENCODE, [CONTROL]) == CONTROL


def test_control_keeps_the_bare_registry_key_and_app_bots_get_a_suffix() -> None:
    assert scoped("webex.room", CONTROL) == "webex.room"
    assert scoped("webex.room", "cursor") == "webex.room:cursor"


def test_a_scope_keeps_only_its_apps_and_control_keeps_everything() -> None:
    sessions = [_session("a", Harness.VSCODE), _session("b", Harness.CURSOR), _session("c", Harness.OPENCODE)]
    assert [s.session_key for s in BotScope.for_bot("cursor").keep(sessions)] == ["b"]
    assert [s.session_key for s in BotScope.for_bot(CONTROL).keep(sessions)] == ["a", "b", "c"]


# two bots end to end


@dataclass
class Space:
    webex: FakeWebex
    frontend: WebexFrontend
    client: httpx.AsyncClient

    def texts(self) -> list[str]:
        return [m.markdown for m in self.webex.bot_messages()]


@dataclass
class TwoBots:
    registry: Registry
    router: Router
    service: RelayService
    renderers: BotRenderers
    spaces: dict[BotName, Space]
    copilot: CopilotAdapter
    opencode: EmittingAdapter

    async def say(self, bot: BotName, text: str, parent_id: str | None = None) -> None:
        space = self.spaces[bot]
        message_id = space.webex.user_says(text, parent_id=parent_id)
        await space.frontend.handle_message(await space.frontend.api.get_message(message_id))
        await self.router.join()


async def _space(
    bot: BotName,
    registry: Registry,
    service: RelayService,
    renderers: BotRenderers,
    config: RelayConfig,
    clock: FakeClock,
) -> Space:
    webex = FakeWebex(name="" if bot == CONTROL else bot)
    client = webex.client()
    api = WebexApi(client, sleep=clock.sleep)
    outbox = Outbox(api, registry, config.webex.allowed_email, bot)
    renderer = WebexRenderer(
        outbox, registry, config.renderer, config.approvals.deadline_s, clock, clock.sleep
    )
    renderers.add(renderer)
    handlers = Handlers(service, renderer, scope=BotScope.for_bot(bot), sessions=renderers)
    access = AccessControl(registry, config.webex.allowed_email, webex.bot_id)
    frontend = WebexFrontend(
        api, registry, service, renderer, handlers, access, webex.bot_id, sleep=clock.sleep
    )
    return Space(webex, frontend, client)


async def _relay(
    registry: Registry, tmp_path_factory: pytest.TempPathFactory, bots: list[BotName]
) -> TwoBots:
    workspace = tmp_path_factory.mktemp("ws")
    save_workspaces(WorkspacesConfig(workspaces=[Workspace(name="proj", path=str(workspace))]))
    config = RelayConfig()
    config.webex.allowed_email = "me@example.com"
    router = Router()
    service = RelayService(registry, router, Signals(), config)
    copilot, opencode = CopilotAdapter(count=0), EmittingAdapter(count=0)
    router.add_adapter(copilot)
    router.add_adapter(opencode)
    renderers = BotRenderers(registry)
    router.subscribe(renderers)
    clock = FakeClock()
    spaces: dict[BotName, Space] = {}
    for bot in bots:
        spaces[bot] = await _space(bot, registry, service, renderers, config, clock)
    await registry.kv_set(PERSON_KEY, "user-person")
    return TwoBots(registry, router, service, renderers, spaces, copilot, opencode)


@pytest.fixture
async def two(registry: Registry, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[TwoBots]:
    relay = await _relay(registry, tmp_path_factory, [CONTROL, "vscode"])
    yield relay
    await relay.router.stop()
    for space in relay.spaces.values():
        await space.client.aclose()


@pytest.fixture
async def single(registry: Registry, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[TwoBots]:
    relay = await _relay(registry, tmp_path_factory, [CONTROL])
    yield relay
    await relay.router.stop()
    await relay.spaces[CONTROL].client.aclose()


async def test_new_from_control_opens_the_thread_in_the_apps_space_and_says_so(two: TwoBots) -> None:
    await two.say(CONTROL, '/new vscode_copilot proj m1 "hi"')
    session = await two.service.session("vscode_copilot:fake1")
    assert session.webex_bot == "vscode"
    vscode = two.spaces["vscode"].webex
    assert session.webex_thread_id in vscode.messages
    assert vscode.messages[session.webex_thread_id].room_id == vscode.direct_room
    assert "🧵 Started in the VS Code space." in two.spaces[CONTROL].texts()


async def test_a_session_of_an_app_without_a_bot_stays_in_control(two: TwoBots) -> None:
    await two.say(CONTROL, '/new opencode proj m1 "hi"')
    session = await two.service.session("opencode:fake1")
    assert session.webex_bot == CONTROL
    assert session.webex_thread_id in two.spaces[CONTROL].webex.messages
    assert not any("Started in" in t for t in two.spaces[CONTROL].texts())


async def test_events_follow_the_bot_recorded_on_the_session(two: TwoBots) -> None:
    await two.say(CONTROL, '/new vscode_copilot proj m1 "hi"')
    before = len(two.spaces[CONTROL].webex.bot_messages())
    await two.renderers.handle(
        TextDelta(harness=Harness.VSCODE_COPILOT, session_key="vscode_copilot:fake1", text="working")
    )
    assert len(two.spaces[CONTROL].webex.bot_messages()) == before
    assert two.renderers.owner(await two.service.session("vscode_copilot:fake1")) == "vscode"


async def test_an_old_thread_without_a_bot_keeps_using_control(two: TwoBots) -> None:
    old = _session("vscode_copilot:old", Harness.VSCODE_COPILOT, thread="msg-legacy")
    fresh = _session("vscode_copilot:new", Harness.VSCODE_COPILOT)
    stamped = _session("vscode_copilot:stamped", Harness.VSCODE_COPILOT, thread="vscode-msg-9", bot="vscode")
    assert two.renderers.owner(old) == CONTROL
    assert two.renderers.owner(fresh) == "vscode"
    assert two.renderers.owner(stamped) == "vscode"


async def test_a_bot_that_is_no_longer_configured_falls_back_to_control(two: TwoBots) -> None:
    gone = _session("cursor:x", Harness.CURSOR, thread="cursor-msg-1", bot="cursor")
    assert two.renderers.owner(gone) == CONTROL
    assert two.renderers.for_session(gone).bot == CONTROL


async def test_a_reply_in_an_app_thread_reaches_the_session(two: TwoBots) -> None:
    await two.say(CONTROL, '/new vscode_copilot proj m1 "hi"')
    session = await two.service.session("vscode_copilot:fake1")
    assert session.webex_thread_id is not None
    await two.say("vscode", "and the tests too", parent_id=session.webex_thread_id)
    queued = [q.text for q in await two.service.queued(session.session_key)]
    sent = [text for _, text in two.copilot.prompts]
    assert "and the tests too" in queued + sent


async def test_sessions_in_an_app_space_lists_only_that_app(two: TwoBots) -> None:
    await two.say(CONTROL, '/new vscode_copilot proj m1 "one"')
    await two.say(CONTROL, '/new opencode proj m1 "two"')
    await two.say("vscode", "/sessions")
    _, card = two.spaces["vscode"].webex.last_card()
    assert choice_values(input_by_id(card, "session")) == ["vscode_copilot:fake1"]
    await two.say(CONTROL, "/sessions")
    _, card = two.spaces[CONTROL].webex.last_card()
    assert set(choice_values(input_by_id(card, "session"))) == {"vscode_copilot:fake1", "opencode:fake1"}


async def test_new_in_an_app_space_refuses_another_apps_session(two: TwoBots) -> None:
    await two.say("vscode", '/new opencode proj m1 "hi"')
    assert two.opencode.started == []
    assert any("start it there" in t for t in two.spaces["vscode"].texts())


async def test_new_card_in_an_app_space_offers_only_its_apps(two: TwoBots) -> None:
    await two.say("vscode", "/new")
    _, card = two.spaces["vscode"].webex.last_card()
    assert len(show_cards(card)) == 1 and "model_vscode_copilot" in _input_ids(card)
    await two.say(CONTROL, "/new")
    _, card = two.spaces[CONTROL].webex.last_card()
    assert len(show_cards(card)) == 2
    assert {"model_vscode_copilot", "model_opencode"} <= _input_ids(card)


async def test_global_commands_work_from_an_app_space(two: TwoBots) -> None:
    await two.say("vscode", "/away on")
    assert await away.is_away(two.registry, None)
    await two.say("vscode", "/away off")
    assert not await away.is_away(two.registry, None)
    before = len(two.spaces["vscode"].texts())
    await two.say("vscode", "/panic now")
    assert paths.pause_flag().exists()
    assert len(two.spaces["vscode"].texts()) == before + 1
    assert len(two.spaces[CONTROL].texts()) == 0


async def test_each_bot_keeps_its_own_room_and_seen_keys(two: TwoBots) -> None:
    await two.say(CONTROL, "/help")
    await two.say("vscode", "/help")
    assert await two.registry.kv_get("webex.room_id") == two.spaces[CONTROL].webex.direct_room
    assert await two.registry.kv_get("webex.room_id:vscode") == two.spaces["vscode"].webex.direct_room


async def test_app_bot_says_hello_once_and_control_never_does(two: TwoBots) -> None:
    vscode = two.spaces["vscode"]
    assert await vscode.frontend.hello() is True
    assert "VS Code" in vscode.texts()[-1] and "vscode_copilot" in vscode.texts()[-1]
    assert await vscode.frontend.hello() is False
    assert len(vscode.texts()) == 1
    assert await two.spaces[CONTROL].frontend.hello() is False


# single bot: nothing changes


async def test_single_bot_keeps_every_app_in_one_space(single: TwoBots) -> None:
    await single.say(CONTROL, '/new vscode_copilot proj m1 "hi"')
    session = await single.service.session("vscode_copilot:fake1")
    assert session.webex_bot == CONTROL
    assert session.webex_thread_id in single.spaces[CONTROL].webex.messages
    assert not any("Started in" in t for t in single.spaces[CONTROL].texts())
    record = SessionRecord(
        session_key="opencode:x", harness=Harness.OPENCODE, harness_id="x", origin=Origin.DESK
    )
    assert single.renderers.owner(record) == CONTROL
