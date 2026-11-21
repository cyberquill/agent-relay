"""Discovery: apps, the shared model cache, and the agent list `/new` offers."""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_relay.adapters.base import ModelInfo
from agent_relay.core import updates
from agent_relay.core.registry import Registry
from agent_relay.core.router import Router
from agent_relay.core.service import AgentOffer, RelayError, RelayService
from agent_relay.core.service import discovery as disc
from agent_relay.core.service.common import STICKY_MODEL_KEY
from agent_relay.core.signals import Signals
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.config import RelayConfig
from agent_relay.models.events import Harness
from tests.core.discovery_fakes import App, Failing, Hanging, Raising, models
from tests.fakes.adapters import EmittingAdapter


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def build(registry: Registry, *apps: EmittingAdapter) -> tuple[RelayService, Clock]:
    router = Router()
    for app in apps:
        router.add_adapter(app)
    service = RelayService(registry, router, Signals(), RelayConfig())
    clock = Clock()
    service._discovery.clock = clock
    return service, clock


def values(offer: AgentOffer) -> list[str]:
    return [e.value for e in offer.entries]


# the shared model cache


async def test_models_come_from_a_ten_minute_cache_and_refresh_asks_again(registry: Registry) -> None:
    app = App(Harness.OPENCODE)
    service, clock = build(registry, app)
    await service.models("opencode")
    await service.models("opencode")
    assert app.listings == 1
    clock.now += disc.MODELS_TTL_S - 1
    await service.models("opencode")
    assert app.listings == 1
    clock.now += 2
    await service.models("opencode")
    assert app.listings == 2
    await service.models("opencode", refresh=True)
    assert app.listings == 3


async def test_two_callers_at_once_share_one_listing(registry: Registry) -> None:
    app = App(Harness.OPENCODE)
    service, _ = build(registry, app)
    first, second = await asyncio.gather(service.models("opencode"), service.models("opencode"))
    assert first == second and app.listings == 1


async def test_a_slow_listing_times_out_but_still_fills_the_cache(registry: Registry) -> None:
    app = Hanging(Harness.OPENCODE)
    service, _ = build(registry, app)
    with pytest.raises(RelayError, match="did not list its models in time"):
        await service._discovery.models("opencode", timeout=0.02)
    app.release.set()
    await asyncio.sleep(0.01)
    assert [m.id for m in await service.models("opencode")] == ["m0", "m1", "m2"]
    assert app.listings == 1


async def test_an_app_that_cannot_list_says_so_as_a_relay_error(registry: Registry) -> None:
    service, _ = build(registry, Failing(Harness.OPENCODE))
    with pytest.raises(RelayError, match="the app is closed"):
        await service.models("opencode")


async def test_an_app_without_model_selection_refuses(registry: Registry) -> None:
    service, _ = build(registry, App(Harness.CURSOR_CLI, picks=False))
    with pytest.raises(RelayError, match="does not offer model selection"):
        await service.models("cursor")


async def test_the_sticky_model_survives_a_listing_that_fails(registry: Registry) -> None:
    service, _ = build(registry, Failing(Harness.OPENCODE))
    await registry.kv_set(STICKY_MODEL_KEY.format(harness="opencode"), "m1")
    assert await service.sticky_model(Harness.OPENCODE) == "m1"


async def test_a_sticky_model_the_app_no_longer_lists_is_dropped(registry: Registry) -> None:
    service, _ = build(registry, App(Harness.OPENCODE))
    await registry.kv_set(STICKY_MODEL_KEY.format(harness="opencode"), "gone")
    assert await service.sticky_model(Harness.OPENCODE) is None


async def test_the_health_checks_of_all_apps_run_side_by_side(registry: Registry) -> None:
    """If they ran one after the other, the first would wait for the second forever."""
    started = 0
    both = asyncio.Event()

    class Meeting(App):
        async def health(self) -> tuple[bool, str]:
            nonlocal started
            started += 1
            if started == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            return True, "fake"

    service, _ = build(registry, Meeting(Harness.OPENCODE), Meeting(Harness.CURSOR_CLI))
    assert [a.healthy for a in await service.apps()] == [True, True]


# Cursor CDP: its model list lives in a menu in the desktop UI


async def test_cdp_models_are_never_listed_for_a_card_or_a_session_start(registry: Registry) -> None:
    cdp = App(Harness.CURSOR_CDP, models(4))
    service, _ = build(registry, cdp)
    assert await service.models("cursor-ide") == []
    offer = await service.agent_offer()
    assert values(offer) == ["cursor_cdp:default"]
    assert await service.sticky_model(Harness.CURSOR_CDP) is None
    assert cdp.listings == 0


async def test_an_explicit_listing_saves_the_cdp_models_for_the_cards(registry: Registry) -> None:
    cdp = App(Harness.CURSOR_CDP, models(4))
    service, _ = build(registry, cdp)
    listed = await service.models("cursor-ide", refresh=True)
    assert [m.id for m in listed] == ["m0", "m1", "m2", "m3"] and cdp.listings == 1
    saved = await registry.kv_get(disc.CDP_MODELS_KEY)
    assert saved is not None and "Model 3" in saved
    assert values(await service.agent_offer()) == [
        "cursor_cdp:default",
        "cursor_cdp:m1",
        "cursor_cdp:m2",
        "cursor_cdp:m3",
    ]
    assert [m.id for m in await service.models("cursor-ide")] == ["m0", "m1", "m2", "m3"]
    assert cdp.listings == 1


async def test_a_garbled_saved_cdp_list_reads_as_empty(registry: Registry) -> None:
    service, _ = build(registry, App(Harness.CURSOR_CDP))
    await registry.kv_set(disc.CDP_MODELS_KEY, "{not json")
    assert await service.models("cursor-ide") == []
    await registry.kv_set(disc.CDP_MODELS_KEY, '[{"nope": 1}]')
    assert await service.models("cursor-ide") == []


# the agent list


async def test_each_app_shows_its_default_first_then_its_models(registry: Registry) -> None:
    service, _ = build(registry, App(Harness.OPENCODE), App(Harness.CURSOR_CLI, models(2, default=None)))
    offer = await service.agent_offer()
    assert values(offer) == [
        "cursor_cli:default",
        "cursor_cli:m0",
        "cursor_cli:m1",
        "opencode:default",
        "opencode:m1",
        "opencode:m2",
    ]
    labels = {e.value: e.label for e in offer.entries}
    assert labels["opencode:default"] == "OpenCode · Default (Model 0)"
    assert labels["cursor_cli:default"] == "Cursor CLI · Default"
    assert labels["opencode:m1"] == "OpenCode · Model 1"
    assert offer.unavailable == []


async def test_an_app_that_cannot_choose_a_model_offers_only_its_default(registry: Registry) -> None:
    service, _ = build(registry, App(Harness.CURSOR_CLI, picks=False))
    assert values(await service.agent_offer()) == ["cursor_cli:default"]


async def test_apps_that_cannot_start_from_the_phone_are_left_out(registry: Registry) -> None:
    watcher = App(Harness.COPILOT_CLI)
    watcher.capabilities = Capabilities(model_select=True)
    service, _ = build(registry, watcher, App(Harness.OPENCODE))
    assert {e.harness for e in (await service.agent_offer()).entries} == {"opencode"}


async def test_an_app_shows_at_most_eight_models_and_hides_its_helpers(registry: Registry) -> None:
    listed = [
        *models(60),
        ModelInfo(id="copilot-utility-small", label="Utility"),
        ModelInfo(id="x", label="dictation cleanup"),
    ]
    service, _ = build(registry, App(Harness.OPENCODE, listed))
    offer = await service.agent_offer()
    assert values(offer) == ["opencode:default", *[f"opencode:m{i}" for i in range(1, 9)]]


async def test_the_whole_list_is_capped_and_every_app_keeps_its_default(registry: Registry) -> None:
    harnesses = [
        Harness.VSCODE,
        Harness.VSCODE_COPILOT,
        Harness.COPILOT_CLI,
        Harness.CURSOR,
        Harness.CURSOR_CLI,
        Harness.OPENCODE,
    ]
    service, _ = build(registry, *[App(h, models(60)) for h in harnesses])
    offer = await service.agent_offer()
    assert len(offer.entries) == disc.OFFER_MAX_ENTRIES
    for h in harnesses:
        assert f"{h.value}:default" in values(offer)
    order = [e.harness for e in offer.entries]
    assert order == sorted(order), "entries are grouped by app"
    per_app = {h.value: order.count(h.value) for h in harnesses}
    assert max(per_app.values()) - min(per_app.values()) <= 1, "the models are shared out evenly"


async def test_a_model_used_before_stays_on_the_list_even_past_the_cap(registry: Registry) -> None:
    service, _ = build(registry, App(Harness.OPENCODE, models(60)))
    await registry.kv_set(STICKY_MODEL_KEY.format(harness="opencode"), "m50")
    offer = await service.agent_offer()
    assert "opencode:m50" in values(offer) and len(offer.entries) == 9


async def test_the_preselection_is_the_last_used_app_with_its_last_model(registry: Registry) -> None:
    service, _ = build(registry, App(Harness.OPENCODE), App(Harness.CURSOR_CLI))
    assert (await service.agent_offer()).preselected == "cursor_cli:default"  # nothing used yet: the first
    await registry.kv_set(disc.LAST_APP_KEY, "opencode")
    assert (await service.agent_offer()).preselected == "opencode:default"
    await registry.kv_set(STICKY_MODEL_KEY.format(harness="opencode"), "m2")
    assert (await service.agent_offer()).preselected == "opencode:m2"
    await registry.kv_set(STICKY_MODEL_KEY.format(harness="opencode"), "gone")
    assert (await service.agent_offer()).preselected == "opencode:default"
    await registry.kv_set(disc.LAST_APP_KEY, "vscode")  # not enabled
    assert (await service.agent_offer()).preselected == "cursor_cli:default"


async def test_an_unhealthy_app_is_left_out_and_named(registry: Registry) -> None:
    down = App(Harness.CURSOR_CLI, healthy=False)
    service, _ = build(registry, App(Harness.OPENCODE), down, Raising(Harness.VSCODE))
    offer = await service.agent_offer()
    assert {e.harness for e in offer.entries} == {"opencode"}
    assert offer.unavailable == ["Cursor CLI (down)", "VS Code (down)"]


async def test_an_app_paused_after_an_update_is_named_with_its_state(registry: Registry) -> None:
    service, _ = build(registry, App(Harness.VSCODE_COPILOT), App(Harness.OPENCODE))
    await updates.set_degraded(registry, Harness.VSCODE_COPILOT, "self-test failed")
    offer = await service.agent_offer()
    assert offer.unavailable == ["VS Code · Copilot (paused)"]
    assert {e.harness for e in offer.entries} == {"opencode"}


async def test_a_slow_app_does_not_delay_the_others_and_gets_only_its_default(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(disc, "OFFER_MODEL_TIMEOUT_S", 0.05)
    slow = Hanging(Harness.CURSOR_CLI)
    service, _ = build(registry, App(Harness.OPENCODE), slow)
    began = time.monotonic()
    offer = await service.agent_offer()
    assert time.monotonic() - began < 1.0
    assert values(offer) == [
        "cursor_cli:default",
        "opencode:default",
        "opencode:m1",
        "opencode:m2",
    ]
    slow.release.set()
    await asyncio.sleep(0.01)
    assert "cursor_cli:m1" in values(await service.agent_offer()), "the late listing filled the cache"
    assert slow.listings == 1


async def test_a_hanging_health_check_is_given_up_on(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(disc, "HEALTH_TIMEOUT_S", 0.05)
    service, _ = build(registry, Hanging(Harness.CURSOR_CLI, hang_health=True), App(Harness.OPENCODE))
    monkeypatch.setattr(disc, "OFFER_MODEL_TIMEOUT_S", 0.05)
    offer = await asyncio.wait_for(service.agent_offer(), 2)
    assert offer.unavailable == ["Cursor CLI (down)"]


async def test_the_second_offer_uses_the_cache(registry: Registry) -> None:
    app = App(Harness.OPENCODE)
    service, _ = build(registry, app)
    await service.agent_offer()
    await service.agent_offer()
    assert app.listings == 1


async def test_no_app_at_all_is_an_empty_offer(registry: Registry) -> None:
    service, _ = build(registry)
    offer = await service.agent_offer()
    assert offer.entries == [] and offer.preselected is None and offer.find("x:default") is None
