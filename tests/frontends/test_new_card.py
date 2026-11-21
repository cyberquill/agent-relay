"""`/new` as one flat card: workspace, agent (app and model in one list), prompt, Start."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay.adapters.base import ModelInfo
from agent_relay.core.service import RelayError
from agent_relay.core.service import discovery as disc
from agent_relay.core.service.common import STICKY_MODEL_KEY
from agent_relay.frontends import card_state
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.handlers import LAST_WORKSPACE_KEY
from agent_relay.models.attachments import Attachment, Received
from agent_relay.models.capabilities import Capabilities
from agent_relay.models.config import Workspace, WorkspacesConfig
from agent_relay.models.events import Harness
from agent_relay.models.sessions import SessionRecord
from agent_relay.settings import load_workspaces, save_workspaces
from tests.core.discovery_fakes import App, Hanging
from tests.frontends.card_spec import check_card
from tests.frontends.card_tools import choice_titles, choice_values, input_by_id, inputs_of
from tests.frontends.conftest import Relay, submit_data

UTILITY = [
    ModelInfo(id="copilot-utility-small", label="Utility"),
    ModelInfo(id="x-1", label="copilot-dictation-cleanup-luna"),
]
OFFERED = ["opencode:default", "opencode:m2"]


def cards_sent(relay: Relay) -> int:
    return sum(1 for m in relay.webex.bot_messages() if m.attachments)


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


def card_deleted(relay: Relay, message_id: str) -> bool:
    return relay.webex.messages[message_id].deleted


class FakeInbox:
    """Files held at the top level, and what the `/new` takes from them."""

    def __init__(self, count: int) -> None:
        self.received = Received(
            attachments=tuple(
                Attachment(
                    name=f"f{i}.png", mime="image/png", size=3, local_path=f"/tmp/f{i}.png", sha256="0" * 64
                )
                for i in range(count)
            )
        )
        self.taken = 0
        self.claimed: list[str] = []
        self.restored = 0

    async def held_top_level(self) -> Received | None:
        return self.received if self.received.attachments else None

    async def take_for_new(self, workspace: Path) -> Received | None:
        self.taken += 1
        return self.received if self.received.attachments else None

    async def claim(self, session_key: str, received: Received) -> None:
        self.claimed.append(session_key)

    async def restore_new(self, received: Received) -> None:
        self.restored += 1


# the card


async def test_bare_new_is_one_flat_card_with_workspace_agent_and_prompt(relay: Relay) -> None:
    await relay.say("/new")
    assert cards_sent(relay) == 1
    _, card = relay.webex.last_card()
    assert cards.card_fallback(card) == "New session"
    assert check_card(card) == []
    assert "Action.ShowCard" not in json.dumps(card) and "Input.Toggle" not in json.dumps(card)
    assert input_ids(card) == {"ws", "agent", "prompt"}
    workspace = input_by_id(card, "ws")
    assert (
        workspace["style"] == "compact" and workspace["value"] == "proj" and workspace["label"] == "Workspace"
    )
    agent = input_by_id(card, "agent")
    assert agent["style"] == "compact" and agent["label"] == "Agent"
    assert choice_values(agent) == OFFERED and agent["value"] == "opencode:default"
    assert choice_titles(agent) == ["🟩 OpenCode · Default (Model One)", "🟩 OpenCode · Model Two"]
    assert submit_data(card, "Start")["relay"] == "new"


async def test_the_prompt_is_required_multiline_and_labelled(relay: Relay) -> None:
    await relay.say("/new")
    _, card = relay.webex.last_card()
    prompt = input_by_id(card, "prompt")
    assert prompt["isMultiline"] is True and prompt["isRequired"] is True
    assert prompt["label"] == "Prompt" and prompt["errorMessage"]
    assert input_by_id(card, "ws")["isRequired"] is True and input_by_id(card, "agent")["isRequired"] is True


async def test_the_offer_is_stored_under_the_cards_nonce(relay: Relay) -> None:
    await relay.say("/new")
    _, card = relay.webex.last_card()
    nonce = str(submit_data(card, "Start")["nonce"])
    assert await card_state.load_offer(relay.registry, nonce) == OFFERED


# starting from the card


async def test_start_from_the_card_starts_the_session_deletes_the_card_and_opens_the_thread(
    relay: Relay,
) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Start", prompt="fix it", agent="opencode:m2")
    started = relay.adapter.started[-1]
    assert started.prompt == "fix it" and started.model == "m2"
    assert started.workspace == load_workspaces().workspaces[0].path
    session = (await relay.service.sessions())[0]
    assert session.webex_thread_id and session.origin.value == "phone"
    assert await relay.service.sticky_model(Harness.OPENCODE) == "m2"
    assert card_deleted(relay, msg.id)
    assert not any("Started" in t for t in relay.bot_texts()), "the thread root is the confirmation"
    assert await card_state.load_offer(relay.registry, str(submit_data(card, "Start")["nonce"])) == []


async def test_the_default_agent_starts_with_the_apps_own_default_not_the_last_model(relay: Relay) -> None:
    await relay.registry.kv_set(STICKY_MODEL_KEY.format(harness="opencode"), "m2")
    await relay.registry.kv_set(disc.LAST_APP_KEY, "opencode")
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    assert input_by_id(card, "agent")["value"] == "opencode:m2"
    await relay.tap(msg.id, card, "Start", prompt="go", agent="opencode:default")
    assert relay.adapter.started[-1].model is None
    assert await relay.registry.kv_get(STICKY_MODEL_KEY.format(harness="opencode")) is None


async def test_the_last_used_app_and_model_are_preselected(relay: Relay) -> None:
    relay.router.add_adapter(App(Harness.CURSOR_CLI))
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    assert input_by_id(card, "agent")["value"] == "cursor_cli:default"  # nothing used yet: the first
    await relay.tap(msg.id, card, "Start", prompt="go", agent="opencode:m2")
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert input_by_id(card, "agent")["value"] == "opencode:m2"


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


async def test_a_one_line_new_is_unchanged_and_starts_without_a_card(relay: Relay) -> None:
    await relay.say('/new opencode proj m2 "run it"')
    assert cards_sent(relay) == 0
    started = relay.adapter.started[-1]
    assert started.prompt == "run it" and started.model == "m2"


async def test_an_empty_prompt_is_refused_and_the_card_stays_usable(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Start", prompt="   ")
    assert relay.bot_texts()[-1] == "⚠️ Say what the session should do."
    assert relay.adapter.started == [] and not card_deleted(relay, msg.id)
    await relay.tap(msg.id, card, "Start", prompt="now")
    assert [s.prompt for s in relay.adapter.started] == ["now"]


async def test_the_card_is_checked_again_on_the_server(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()

    async def tap(**extra: JsonValue) -> str:
        await relay.tap(msg.id, card, "Start", **{"prompt": "go", **extra})
        return relay.bot_texts()[-1]

    assert "Unknown workspace `elsewhere`" in await tap(ws="elsewhere")
    assert "not one this card offered" in await tap(agent="opencode:m9")
    assert "not one this card offered" in await tap(agent="vscode:default")
    assert "not one this card offered" in await tap(agent="")
    assert "unexpected" in await tap(agent=7)
    assert "too long" in await tap(prompt="x" * (cards.MAX_PROMPT_CHARS + 1))
    assert "unexpected" in await tap(ws=7)
    assert "Pick a workspace" in await tap(ws="")
    assert relay.adapter.started == [] and not card_deleted(relay, msg.id)
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert [s.prompt for s in relay.adapter.started] == ["go"]


async def test_a_forged_agent_is_refused_even_for_an_enabled_app_model(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(
        msg.id, card, "Start", prompt="go", agent="opencode:m1"
    )  # the default model: only via "default"
    assert "not one this card offered" in relay.bot_texts()[-1]
    assert relay.adapter.started == []


async def test_a_card_whose_offer_is_gone_asks_for_a_new_one(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await card_state.drop_offer(relay.registry, str(submit_data(card, "Start")["nonce"]))
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert "Send `/new` again" in relay.bot_texts()[-1]
    assert relay.adapter.started == []


async def test_the_card_can_start_only_one_session(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Start", prompt="go")
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert len(relay.adapter.started) == 1
    assert relay.bot_texts()[-1] == card_state.EXPIRED


async def test_a_start_that_fails_before_a_session_exists_can_be_tapped_again(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    real = relay.service.start
    failures = [RuntimeError("the app fell over")]

    async def flaky(
        app: str,
        workspace: str,
        prompt: str,
        model: str | None = None,
        attachments: Sequence[Attachment] = (),
    ) -> SessionRecord:
        if failures:
            raise failures.pop()
        return await real(app, workspace, prompt, model, attachments)

    monkeypatch.setattr(relay.service, "start", flaky)
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert relay.bot_texts()[-1].startswith("⚠️ The session could not be started (RuntimeError)")
    assert not card_deleted(relay, msg.id) and relay.adapter.started == []
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert [s.prompt for s in relay.adapter.started] == ["go"]
    assert card_deleted(relay, msg.id)


async def test_a_relay_error_from_the_start_is_shown_as_is_and_keeps_the_card(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()

    async def refuse(*_: object, **__: object) -> None:
        raise RelayError("OpenCode is paused: self-test failed.")

    monkeypatch.setattr(relay.service, "start", refuse)
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert relay.bot_texts()[-1] == "⚠️ OpenCode is paused: self-test failed."
    assert not card_deleted(relay, msg.id)


async def test_a_failure_after_the_session_exists_is_not_a_failed_tap(relay: Relay) -> None:
    class Unclaimable(FakeInbox):
        async def claim(self, session_key: str, received: Received) -> None:
            raise RuntimeError("could not record the folder")

    relay.frontend.handlers.inbox = Unclaimable(1)  # type: ignore[assignment]
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert len(relay.adapter.started) == 1 and card_deleted(relay, msg.id)
    await relay.tap(msg.id, card, "Start", prompt="go")
    assert len(relay.adapter.started) == 1 and relay.bot_texts()[-1] == card_state.EXPIRED


# partial commands open the card prefilled


async def test_new_with_pieces_asks_only_for_the_rest_in_the_same_card(relay: Relay) -> None:
    await relay.say("/new opencode proj")
    assert cards_sent(relay) == 1
    _, card = relay.webex.last_card()
    assert input_by_id(card, "ws")["value"] == "proj"
    assert input_by_id(card, "agent")["value"] == "opencode:default"
    await relay.say('/new "write the docs"')
    _, card = relay.webex.last_card()
    assert input_by_id(card, "prompt")["value"] == "write the docs"


async def test_a_typed_model_is_preselected_on_the_card(relay: Relay) -> None:
    await relay.say("/new opencode proj m2")
    _, card = relay.webex.last_card()
    assert input_by_id(card, "agent")["value"] == "opencode:m2"


async def test_a_prompt_too_long_to_prefill_is_left_blank_and_says_so(relay: Relay) -> None:
    await relay.say(f'/new "{"w" * 5000}"')
    _, card = relay.webex.last_card()
    assert "value" not in input_by_id(card, "prompt")
    assert "too long to prefill" in json.dumps(card)


async def test_the_ws_cards_start_button_opens_the_card_with_that_workspace(
    relay: Relay, tmp_path: Path
) -> None:
    add_workspace(tmp_path, "other")
    await relay.say("/help")
    anchor = relay.webex.bot_messages()[-1]
    await relay.click(
        anchor.id, {"relay": "new", "step": "ws", "value": "other", "nonce": await relay.nonce()}
    )
    _, card = relay.webex.last_card()
    assert input_by_id(card, "ws")["value"] == "other"
    assert relay.adapter.started == [] and card_deleted(relay, anchor.id)


async def test_a_new_button_without_an_agent_asks_for_one(relay: Relay) -> None:
    await relay.say("/help")
    anchor = relay.webex.bot_messages()[-1].id
    await relay.click(anchor, {"relay": "new", "step": "prompt", "value": "x", "nonce": await relay.nonce()})
    assert relay.bot_texts()[-1] == "⚠️ Pick an agent."


# what the list shows


async def test_utility_models_are_hidden_in_the_card_and_in_models(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = await relay.adapter.list_models()

    async def listed() -> list[ModelInfo]:
        return [*base, *UTILITY]

    monkeypatch.setattr(relay.adapter, "list_models", listed)
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert choice_values(input_by_id(card, "agent")) == OFFERED
    await relay.say("/models opencode")
    shown = relay.bot_texts()[-1]
    assert "utility" not in shown.lower() and "dictation" not in shown and "`m1`" in shown


async def test_a_sticky_utility_model_is_not_preselected(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = await relay.adapter.list_models()

    async def listed() -> list[ModelInfo]:
        return [*base, *UTILITY]

    monkeypatch.setattr(relay.adapter, "list_models", listed)
    await relay.registry.kv_set(STICKY_MODEL_KEY.format(harness="opencode"), "copilot-utility-small")
    await relay.registry.kv_set(disc.LAST_APP_KEY, "opencode")
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert input_by_id(card, "agent")["value"] == "opencode:default"


async def test_two_apps_one_with_models_and_one_without(relay: Relay) -> None:
    relay.router.add_adapter(App(Harness.CURSOR_CLI, picks=False))
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    agent = input_by_id(card, "agent")
    assert choice_values(agent) == ["cursor_cli:default", *OFFERED]
    assert choice_titles(agent)[0] == "⬛ Cursor CLI · Default"
    await relay.tap(msg.id, card, "Start", prompt="go", agent="cursor_cli:default")
    assert (await relay.service.sessions())[0].harness is Harness.CURSOR_CLI


async def test_an_unhealthy_app_is_left_out_with_one_subtle_line(relay: Relay) -> None:
    relay.router.add_adapter(App(Harness.CURSOR_CLI, healthy=False))
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert choice_values(input_by_id(card, "agent")) == OFFERED
    body = card["body"]
    assert isinstance(body, list)
    notes = [b for b in body if isinstance(b, dict) and str(b.get("text", "")).startswith("Not available")]
    assert [n["text"] for n in notes] == ["Not available: Cursor CLI (down)"] and notes[0]["isSubtle"] is True


async def test_a_slow_app_does_not_delay_the_card_and_the_cache_serves_the_next_one(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(disc, "OFFER_MODEL_TIMEOUT_S", 0.05)
    slow = Hanging(Harness.CURSOR_CLI)
    relay.router.add_adapter(slow)
    await asyncio.wait_for(relay.say("/new"), 3)
    _, card = relay.webex.last_card()
    assert choice_values(input_by_id(card, "agent")) == ["cursor_cli:default", *OFFERED]
    slow.release.set()
    await asyncio.sleep(0.01)
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert "cursor_cli:m1" in choice_values(input_by_id(card, "agent")) and slow.listings == 1


async def test_the_second_new_uses_the_cached_model_lists(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    base = await relay.adapter.list_models()

    async def counted() -> list[ModelInfo]:
        nonlocal calls
        calls += 1
        return base

    monkeypatch.setattr(relay.adapter, "list_models", counted)
    await relay.say("/new")
    await relay.say("/new")
    assert calls == 1


async def test_cursor_cdp_never_opens_its_model_menu_for_a_card(relay: Relay) -> None:
    cdp = App(Harness.CURSOR_CDP, [ModelInfo(id="Sonnet", label="Sonnet"), ModelInfo(id="GPT", label="GPT")])
    relay.router.add_adapter(cdp)
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert "cursor_cdp:default" in choice_values(input_by_id(card, "agent"))
    assert "cursor_cdp:Sonnet" not in choice_values(input_by_id(card, "agent")) and cdp.listings == 0
    await relay.say("/models cursor-ide")
    assert cdp.listings == 1
    await relay.say("/new")
    _, card = relay.webex.last_card()
    assert "cursor_cdp:Sonnet" in choice_values(input_by_id(card, "agent")) and cdp.listings == 1


async def test_files_waiting_for_the_next_new_are_announced_and_go_with_it(relay: Relay) -> None:
    inbox = FakeInbox(2)
    relay.frontend.handlers.inbox = inbox  # type: ignore[assignment]
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    assert "📎 2 files will be attached" in json.dumps(card, ensure_ascii=False)
    await relay.tap(msg.id, card, "Start", prompt="look at these")
    assert inbox.taken == 1 and len(relay.adapter.start_attachments[-1]) == 2
    assert inbox.claimed == ["opencode:fake1"]


async def test_a_file_sent_at_the_top_level_is_announced_on_the_card_and_goes_with_start(
    relay: Relay,
) -> None:
    message_id = relay.webex.user_sends_file("a.txt", b"1", "", None)
    await relay.frontend.handle_message(await relay.frontend.api.get_message(message_id))
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    assert "📎 1 file will be attached" in json.dumps(card, ensure_ascii=False)
    await relay.tap(msg.id, card, "Start", prompt="read it")
    assert [a.name for a in relay.adapter.start_attachments[-1]] == ["a.txt"]


async def test_one_file_is_announced_in_the_singular_and_none_says_nothing(relay: Relay) -> None:
    relay.frontend.handlers.inbox = FakeInbox(1)  # type: ignore[assignment]
    await relay.say("/new")
    assert "📎 1 file will be attached" in json.dumps(relay.webex.last_card()[1], ensure_ascii=False)
    relay.frontend.handlers.inbox = FakeInbox(0)  # type: ignore[assignment]
    await relay.say("/new")
    assert "📎" not in json.dumps(relay.webex.last_card()[1], ensure_ascii=False)


async def test_a_start_that_fails_gives_the_held_files_back(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = FakeInbox(1)
    relay.frontend.handlers.inbox = inbox  # type: ignore[assignment]

    async def refuse(*_: object, **__: object) -> None:
        raise RelayError("no")

    monkeypatch.setattr(relay.service, "start", refuse)
    await relay.say('/new opencode proj "go"')
    assert inbox.restored == 1 and relay.bot_texts()[-1] == "⚠️ no"


# edge cases


async def test_no_workspaces_and_no_apps_are_said_plainly(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relay.adapter, "capabilities", Capabilities())
    await relay.say("/new")
    assert "No app that can start sessions" in relay.bot_texts()[-1]
    save_workspaces(WorkspacesConfig(workspaces=[]))
    await relay.say("/new")
    assert relay.bot_texts()[-1].startswith("No workspaces.")


def test_a_long_workspace_list_is_capped_with_a_note_and_the_agent_list_holds_forty() -> None:
    workspaces = [(f"w{i}", f"w{i}") for i in range(30)]
    agents = [(f"opencode:m{i}", f"OpenCode · {i}") for i in range(60)]
    card = cards.new_session(workspaces, "w29", agents, "opencode:m50", None, nonce="n")
    assert len(choice_values(input_by_id(card, "ws"))) == cards.MAX_CHOICES
    assert input_by_id(card, "ws")["value"] == "w29" and "Showing 25 of 30" in json.dumps(card)
    values = choice_values(input_by_id(card, "agent"))
    assert len(values) == cards.AGENT_LIMIT and values[0] == "opencode:m50"
    assert check_card(card) == []
