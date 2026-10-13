from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from pydantic import JsonValue

from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import RelayService
from agent_relay.core.signals import Signals
from agent_relay.frontends import card_state
from agent_relay.frontends.webex.access import PERSON_KEY, AccessControl
from agent_relay.frontends.webex.api import WebexApi
from agent_relay.frontends.webex.bot import WebexFrontend
from agent_relay.frontends.webex.handlers import Handlers
from agent_relay.frontends.webex.renderer import Outbox, WebexRenderer
from agent_relay.models.config import RelayConfig, Workspace, WorkspacesConfig
from agent_relay.settings import save_workspaces
from tests.fakes.adapters import EmittingAdapter
from tests.fakes.webex import FakeWebex
from tests.frontends.card_tools import defaults, find_action


def submit_data(card: dict[str, JsonValue], title: str) -> dict[str, JsonValue]:
    """The `data` of the card's submit button with this title (also inside ShowCards and ActionSets)."""
    data = find_action(card, title).get("data")
    assert isinstance(data, dict)
    return dict(data)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@dataclass
class Relay:
    webex: FakeWebex
    clock: FakeClock
    registry: Registry
    router: Router
    service: RelayService
    frontend: WebexFrontend
    adapter: EmittingAdapter
    config: RelayConfig

    async def say(self, text: str, parent_id: str | None = None) -> None:
        message_id = self.webex.user_says(text, parent_id=parent_id)
        await self.frontend.handle_message(await self.frontend.api.get_message(message_id))
        await self.router.join()

    async def click(self, message_id: str, inputs: dict[str, JsonValue]) -> None:
        action_id = self.webex.user_clicks(message_id, inputs)
        await self.frontend.handle_action(await self.frontend.api.get_attachment_action(action_id))
        await self.router.join()

    async def press(
        self, message_id: str, card: dict[str, JsonValue], title: str, **extra: JsonValue
    ) -> None:
        """Tap a button on a card the bot sent; the card's own data and nonce go with it."""
        await self.click(message_id, {**submit_data(card, title), **extra})

    async def tap(self, message_id: str, card: dict[str, JsonValue], title: str, **extra: JsonValue) -> None:
        """Tap like a client: each input's initial value, then the button's data and nonce, then `extra`."""
        await self.click(message_id, {**defaults(card), **submit_data(card, title), **extra})

    async def nonce(self) -> str:
        return await card_state.issue_nonce(self.registry)

    def bot_texts(self) -> list[str]:
        return [m.markdown for m in self.webex.bot_messages()]


@pytest.fixture
async def relay(registry: Registry, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Relay]:
    workspace = tmp_path_factory.mktemp("ws")
    save_workspaces(WorkspacesConfig(workspaces=[Workspace(name="proj", path=str(workspace))]))
    config = RelayConfig()
    config.webex.allowed_email = "me@example.com"
    webex = FakeWebex()
    clock = FakeClock()
    client = webex.client()
    api = WebexApi(client, sleep=clock.sleep)
    router = Router()
    signals = Signals()
    service = RelayService(registry, router, signals, config)
    adapter = EmittingAdapter(count=0)
    router.add_adapter(adapter)
    outbox = Outbox(api, registry, config.webex.allowed_email)
    renderer = WebexRenderer(
        outbox, registry, config.renderer, config.approvals.deadline_s, clock, clock.sleep
    )
    router.subscribe(renderer)
    handlers = Handlers(service, renderer)
    access = AccessControl(registry, config.webex.allowed_email, webex.bot_id)
    frontend = WebexFrontend(
        api, registry, service, renderer, handlers, access, webex.bot_id, sleep=clock.sleep
    )
    await registry.kv_set(PERSON_KEY, webex.user_id)
    yield Relay(webex, clock, registry, router, service, frontend, adapter, config)
    await router.stop()
    await client.aclose()
