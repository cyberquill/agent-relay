"""`/new` as one card (or two): workspace, prompt, away toggle, one ShowCard per app."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay.adapters.base import ModelInfo
from agent_relay.core import away
from agent_relay.core.registry import GLOBAL_SCOPE
from agent_relay.core.service import RelayError
from agent_relay.core.service.common import STICKY_MODEL_KEY
from agent_relay.frontends import card_state
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.handlers import DEFAULT_MODEL, DRAFT_PREFIX, LAST_WORKSPACE_KEY
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.config import Workspace, WorkspacesConfig
from agent_relay.models.events import Harness
from agent_relay.settings import load_workspaces, save_workspaces
from tests.fakes.adapters import EmittingAdapter
from tests.frontends.card_tools import (
    actions_of,
    choice_values,
    input_by_id,
    inputs_of,
    show_cards,
)
from tests.frontends.conftest import Relay, submit_data

UTILITY = [
    ModelInfo(id="copilot-utility-small", label="Utility"),
    ModelInfo(id="x-1", label="copilot-dictation-cleanup-luna"),
]


class PlainAdapter(EmittingAdapter):
    """An app that can start sessions but has no model choice."""

    harness = Harness.CURSOR_CLI
    capabilities = Capabilities(cold_start=True)


def cards_sent(relay: Relay) -> int:
    return sum(1 for m in relay.webex.bot_messages() if m.attachments)


def body_types(card: dict[str, JsonValue]) -> list[str]:
    body = card["body"]
    assert isinstance(body, list)
    return [str(b["type"]) for b in body if isinstance(b, dict)]


def add_workspace(tmp_path: Path, name: str) -> str:
    folder = tmp_path / name
    folder.mkdir()
    current = load_workspaces()
    save_workspaces(
        WorkspacesConfig(workspaces=[*current.workspaces, Workspace(name=name, path=str(folder))])
    )
    return str(folder)


def input_ids(card: dict[str, JsonValue]) -> set[str]:
    return {str(i["id"]) for i in inputs_of(card)}


async def test_bare_new_is_one_card_with_workspace_prompt_toggle_and_a_showcard_per_app(
    relay: Relay,
) -> None:
    await relay.say("/new")
    assert cards_sent(relay) == 1
    _, card = relay.webex.last_card()
    assert cards.card_fallback(card) == "New session"
    assert "Input.Toggle" in body_types(card)
    workspace = input_by_id(card, "ws")
    assert workspace["style"] == "compact" and workspace["value"] == "proj"
    assert input_by_id(card, "prompt")["isMultiline"] is True
    toggle = input_by_id(card, "away")
    assert toggle["value"] == "false" and toggle["valueOn"] == "true"
    assert list(show_cards(card)) == ["🟩 OpenCode"]
    model = input_by_id(card, "model_opencode")
    assert model["style"] == "compact" and model["value"] == DEFAULT_MODEL
    assert choice_values(model) == [DEFAULT_MODEL, "m1", "m2"]
    assert submit_data(card, "Start")["step"] == "start"


async def test_start_from_the_card_starts_the_session_in_its_thread(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Start", prompt="fix it", model_opencode="m2")
    started = relay.adapter.started[-1]
    assert started.prompt == "fix it" and started.model == "m2"
    assert started.workspace == load_workspaces().workspaces[0].path
    session = (await relay.service.sessions())[0]
    assert session.webex_thread_id and session.origin.value == "phone"
    assert await relay.service.sticky_model(Harness.OPENCODE) == "m2"
    assert cards_sent(relay) == 1


async def test_the_default_model_starts_with_none(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert relay.adapter.started[-1].model is None


async def test_the_sticky_model_is_preselected(relay: Relay) -> None:
    await relay.registry.kv_set(STICKY_MODEL_KEY.format(harness="opencode"), "m2")
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert input_by_id(card, "model_opencode")["value"] == "m2"


async def test_the_last_used_workspace_is_preselected(relay: Relay, tmp_path: Path) -> None:
    add_workspace(tmp_path, "other")
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    assert input_by_id(card, "ws")["value"] == "proj"
    await relay.tap(msg.id, card, "Start", prompt="go", ws="other")
    assert await relay.registry.kv_get(LAST_WORKSPACE_KEY) == "other"
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert input_by_id(card, "ws")["value"] == "other"


async def test_a_one_line_new_also_remembers_the_workspace(relay: Relay, tmp_path: Path) -> None:
    add_workspace(tmp_path, "other")
    await relay.say('/new opencode other "run it"')
    assert await relay.registry.kv_get(LAST_WORKSPACE_KEY) == "other"


async def _with_utility_models(relay: Relay, monkeypatch: pytest.MonkeyPatch) -> None:
    base = await relay.adapter.list_models()

    async def listed() -> list[ModelInfo]:
        return [*base, *UTILITY]

    monkeypatch.setattr(relay.adapter, "list_models", listed)


async def test_utility_models_are_hidden_in_the_card_and_in_models(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _with_utility_models(relay, monkeypatch)
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert choice_values(input_by_id(card, "model_opencode")) == [DEFAULT_MODEL, "m1", "m2"]
    await relay.say("/models opencode")
    shown = relay.bot_texts()[-1]
    assert "utility" not in shown.lower() and "dictation" not in shown and "`m1`" in shown


async def test_a_sticky_utility_model_is_not_preselected(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _with_utility_models(relay, monkeypatch)
    await relay.registry.kv_set(STICKY_MODEL_KEY.format(harness="opencode"), "copilot-utility-small")
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert input_by_id(card, "model_opencode")["value"] == DEFAULT_MODEL


async def test_the_away_toggle_turns_away_on_for_that_workspace(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    workspace = load_workspaces().workspaces[0].path
    assert not await away.is_away(relay.registry, workspace)
    await relay.tap(msg.id, card, "Start", prompt="go", away="true")
    assert await away.is_away(relay.registry, workspace)
    assert not await away.is_away(relay.registry, "/elsewhere")
    assert relay.adapter.started


async def test_an_untouched_toggle_changes_nothing(relay: Relay) -> None:
    await relay.say("/away on")
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert list(await relay.registry.away_scopes()) == [GLOBAL_SCOPE]


async def test_an_empty_prompt_is_refused_and_the_card_stays_usable(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Start", prompt="   ")
    assert relay.bot_texts()[-1] == "⚠️ Say what the session should do."
    assert relay.adapter.started == []
    await relay.tap(msg.id, card, "Start", prompt="now")
    assert [s.prompt for s in relay.adapter.started] == ["now"]


async def test_the_card_is_checked_again_on_the_server(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()

    async def tap(**extra: JsonValue) -> str:
        await relay.tap(msg.id, card, "Start", **{"prompt": "go", **extra})
        return relay.bot_texts()[-1]

    assert "Unknown workspace `elsewhere`" in await tap(ws="elsewhere")
    assert "has no model `m9`" in await tap(model_opencode="m9")
    assert "Unknown app `nope`" in await tap(app="nope")
    assert "Cursor CLI is not enabled" in await tap(app="cursor")
    assert "too long" in await tap(prompt="x" * (cards.MAX_PROMPT_CHARS + 1))
    assert "unexpected" in await tap(ws=7)
    assert "Pick a workspace" in await tap(ws="")
    assert relay.adapter.started == []
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert [s.prompt for s in relay.adapter.started] == ["go"]


async def test_the_card_can_start_only_one_session(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Start", prompt="go")
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert len(relay.adapter.started) == 1
    assert relay.bot_texts()[-1] == card_state.EXPIRED


async def test_a_start_that_fails_after_acting_is_not_replayed(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()

    async def boom(*_: object, **__: object) -> None:
        raise RuntimeError("after the start")

    monkeypatch.setattr(relay.service, "start", boom)
    with pytest.raises(RuntimeError):
        await relay.tap(msg.id, card, "Start", prompt="go")
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert relay.bot_texts()[-1] == card_state.EXPIRED


async def test_new_with_pieces_asks_only_for_the_rest_in_the_same_card(relay: Relay) -> None:
    await relay.say("/new opencode proj")
    assert cards_sent(relay) == 1
    _, card = relay.webex.last_card()
    assert list(show_cards(card)) == ["🟩 OpenCode"]
    assert input_by_id(card, "ws")["value"] == "proj"
    await relay.say('/new "write the docs"')
    _, card = relay.webex.last_card()
    assert input_by_id(card, "prompt")["value"] == "write the docs"
    assert input_by_id(card, "model_opencode")["value"] == DEFAULT_MODEL


async def test_a_prompt_too_long_to_prefill_is_left_blank_and_says_so(relay: Relay) -> None:
    await relay.say(f'/new "{"w" * 5000}"')
    _, card = relay.webex.last_card()
    assert "value" not in input_by_id(card, "prompt")
    assert "too long to prefill" in json.dumps(card)


async def test_a_typed_model_is_preselected(relay: Relay) -> None:
    await relay.say("/new opencode proj m2")
    _, card = relay.webex.last_card()
    assert input_by_id(card, "model_opencode")["value"] == "m2"


async def test_the_step_buttons_other_lanes_send_still_work(relay: Relay) -> None:
    await relay.say("/help")
    anchor = relay.webex.bot_messages()[-1].id
    await relay.click(anchor, {"relay": "new", "step": "ws", "value": "proj", "nonce": await relay.nonce()})
    _, card = relay.webex.last_card()
    assert input_by_id(card, "ws")["value"] == "proj"
    assert relay.adapter.started == []
    steps: dict[str, JsonValue] = {"app": "opencode", "ws": "proj", "nonce": await relay.nonce()}
    await relay.click(anchor, {"relay": "new", "step": "prompt", "value": "do it", **steps})
    assert [s.prompt for s in relay.adapter.started] == ["do it"]


async def test_two_apps_one_with_models_and_one_without(relay: Relay) -> None:
    relay.router.add_adapter(PlainAdapter())
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    nested = show_cards(card)
    assert list(nested) == ["⬛ Cursor CLI", "🟩 OpenCode"]
    assert input_ids(card) == {"ws", "prompt", "away", "model_opencode"}
    plain = submit_data(nested["⬛ Cursor CLI"], "Start")
    assert plain["app"] == "cursor_cli"
    await relay.click(msg.id, {"ws": "proj", "prompt": "go", "away": "false", **plain})
    assert (await relay.service.sessions())[0].harness is Harness.CURSOR_CLI


def test_more_than_five_apps_go_in_a_body_actionset() -> None:
    options = [cards.AppOption(f"app{i}", f"App {i}", "▫️") for i in range(6)]
    card = cards.new_session([("proj", "proj")], "proj", None, options, nonce="n")
    assert card["actions"] == [] and "ActionSet" in body_types(card)
    assert len(show_cards(card)) == 6
    five = cards.new_session([("proj", "proj")], "proj", None, options[:5], nonce="n")
    actions = five["actions"]
    assert isinstance(actions, list) and len(actions) == 5 and "ActionSet" not in body_types(five)


def test_a_long_model_list_is_capped_and_keeps_the_preselected_one() -> None:
    models = tuple((f"m{i}", f"Model {i}") for i in range(60))
    option = cards.AppOption("opencode", "OpenCode", "🟩", models, "m50")
    card = cards.new_session([("proj", "proj")], "proj", None, [option], nonce="n")
    block = input_by_id(card, "model_opencode")
    values = choice_values(block)
    assert len(values) == cards.MAX_CHOICES and "m50" in values and block["value"] == "m50"
    assert "Showing 25 of 60" in json.dumps(card)


async def test_models_that_cannot_be_listed_leave_the_app_on_its_default(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(app: str) -> list[ModelInfo]:
        raise RelayError("OpenCode is down")

    monkeypatch.setattr(relay.service, "models", down)
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert "model_opencode" not in input_ids(card)
    assert "picks its own model" in json.dumps(card)


async def test_no_workspaces_and_no_apps_are_said_plainly(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relay.adapter, "capabilities", Capabilities())
    await relay.say("/new")
    assert "No app that can start sessions" in relay.bot_texts()[-1]
    save_workspaces(WorkspacesConfig(workspaces=[]))
    await relay.say("/new")
    assert relay.bot_texts()[-1].startswith("No workspaces.")


async def test_the_two_card_layout_splits_app_workspace_prompt_from_model(relay: Relay) -> None:
    relay.config.webex.new_card_layout = "two"
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    assert [a["title"] for a in actions_of(card)] == ["Next"]
    assert input_ids(card) == {"app", "ws", "prompt", "away"}
    assert input_by_id(card, "app")["value"] == "opencode"
    await relay.tap(msg.id, card, "Next", prompt="split it", away="true")
    assert relay.adapter.started == []
    msg2, card2 = relay.webex.last_card()
    assert msg2.id != msg.id and cards_sent(relay) == 2
    assert [a["title"] for a in actions_of(card2)] == ["Start"]
    assert choice_values(input_by_id(card2, "model")) == [DEFAULT_MODEL, "m1", "m2"]
    nonce = submit_data(card2, "Start")["nonce"]
    assert await relay.registry.kv_get(f"{DRAFT_PREFIX}{nonce}") is not None
    await relay.tap(msg2.id, card2, "Start", model="m2")
    started = relay.adapter.started[-1]
    assert started.prompt == "split it" and started.model == "m2"
    assert await away.is_away(relay.registry, started.workspace)
    assert await relay.registry.kv_get(f"{DRAFT_PREFIX}{nonce}") is None


async def test_the_two_card_layout_checks_card_one_and_skips_card_two_for_a_plain_app(
    relay: Relay,
) -> None:
    relay.config.webex.new_card_layout = "two"
    relay.router.add_adapter(PlainAdapter())
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Next", prompt="  ")
    assert relay.bot_texts()[-1] == "⚠️ Say what the session should do."
    await relay.tap(msg.id, card, "Next", prompt="go", app="cursor_cli")
    assert cards_sent(relay) == 1
    assert (await relay.service.sessions())[0].harness is Harness.CURSOR_CLI


async def test_a_stale_second_card_says_the_draft_expired(relay: Relay) -> None:
    relay.config.webex.new_card_layout = "two"
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Next", prompt="go")
    msg2, card2 = relay.webex.last_card()
    await relay.registry.kv_delete(f"{DRAFT_PREFIX}{submit_data(card2, 'Start')['nonce']}")
    await relay.tap(msg2.id, card2, "Start")
    assert "draft expired" in relay.bot_texts()[-1]
    assert relay.adapter.started == []


async def test_the_second_cards_model_is_checked_and_the_card_stays_usable(relay: Relay) -> None:
    relay.config.webex.new_card_layout = "two"
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Next", prompt="go")
    msg2, card2 = relay.webex.last_card()
    await relay.tap(msg2.id, card2, "Start", model="m9")
    assert "has no model `m9`" in relay.bot_texts()[-1]
    await relay.tap(msg2.id, card2, "Start", model="m1")
    assert relay.adapter.started[-1].model == "m1"
