"""Bare `/model` in a thread: a picker with the current model preselected."""

from __future__ import annotations

import pytest

from agent_relay.adapters.base import ModelInfo
from agent_relay.core.service import RelayError
from agent_relay.frontends import card_state
from agent_relay.models.events import Harness
from agent_relay.models.sessions import SessionRecord
from tests.fakes.adapters import CrashingAdapter
from tests.frontends.card_tools import choice_values, input_by_id
from tests.frontends.conftest import Relay, submit_data


async def _thread(relay: Relay, model: str = "m1") -> SessionRecord:
    await relay.say(f'/new opencode proj {model} "start"')
    return (await relay.service.sessions())[0]


async def test_bare_model_is_a_picker_in_the_thread_with_the_current_model_selected(relay: Relay) -> None:
    session = await _thread(relay, "m2")
    await relay.say("/model", parent_id=session.webex_thread_id)
    msg, card = relay.webex.last_card()
    assert msg.markdown == "Model: `m2`" and msg.parent_id == session.webex_thread_id
    picker = input_by_id(card, "model")
    assert picker["style"] == "compact" and picker["value"] == "m2"
    assert choice_values(picker) == ["m1", "m2"]
    assert submit_data(card, "Apply")["session"] == session.session_key


async def test_apply_sets_the_model_and_says_so_in_the_thread(relay: Relay) -> None:
    session = await _thread(relay)
    await relay.say("/model", parent_id=session.webex_thread_id)
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Apply", model="m2")
    assert relay.adapter.models[-1] == (session.session_key, "m2")
    last = relay.webex.bot_messages()[-1]
    assert last.markdown == "Model set to `m2`." and last.parent_id == session.webex_thread_id
    assert (await relay.service.session(session.session_key)).model == "m2"


async def test_without_a_current_model_the_apps_default_is_preselected(relay: Relay) -> None:
    await relay.say('/new opencode proj default "start"')
    session = (await relay.service.sessions())[0]
    assert session.model is None
    await relay.say("/model", parent_id=session.webex_thread_id)
    msg, card = relay.webex.last_card()
    assert msg.markdown == "Model: `default`"
    assert input_by_id(card, "model")["value"] == "m1"


async def _with_utility_models(relay: Relay, monkeypatch: pytest.MonkeyPatch) -> None:
    base = await relay.adapter.list_models()

    async def listed() -> list[ModelInfo]:
        return [*base, ModelInfo(id="copilot-utility-small", label="U"), ModelInfo(id="d", label="dictation")]

    monkeypatch.setattr(relay.adapter, "list_models", listed)


async def test_utility_models_are_hidden_and_cannot_be_applied(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _thread(relay)
    await _with_utility_models(relay, monkeypatch)
    await relay.say("/model", parent_id=session.webex_thread_id)
    msg, card = relay.webex.last_card()
    assert choice_values(input_by_id(card, "model")) == ["m1", "m2"]
    await relay.tap(msg.id, card, "Apply", model="copilot-utility-small")
    assert "has no model `copilot-utility-small`" in relay.bot_texts()[-1]
    assert relay.adapter.models == []


async def test_a_model_the_app_does_not_list_is_refused_and_the_card_stays_usable(relay: Relay) -> None:
    session = await _thread(relay)
    await relay.say("/model", parent_id=session.webex_thread_id)
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Apply", model="m9")
    assert "has no model `m9`" in relay.bot_texts()[-1] and relay.adapter.models == []
    await relay.tap(msg.id, card, "Apply", model="")
    assert relay.bot_texts()[-1] == "⚠️ Pick a model first."
    await relay.tap(msg.id, card, "Apply", model="m2")
    assert relay.adapter.models == [(session.session_key, "m2")]


async def test_a_session_that_is_gone_is_refused(relay: Relay) -> None:
    session = await _thread(relay)
    await relay.say("/model", parent_id=session.webex_thread_id)
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Apply", session="opencode:gone", model="m2")
    assert "no longer known" in relay.bot_texts()[-1]


async def test_the_picker_is_single_use(relay: Relay) -> None:
    session = await _thread(relay)
    await relay.say("/model", parent_id=session.webex_thread_id)
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "Apply", model="m2")
    await relay.tap(msg.id, card, "Apply", model="m1")
    assert relay.adapter.models == [(session.session_key, "m2")]
    assert relay.bot_texts()[-1] == card_state.EXPIRED


async def test_when_models_cannot_be_listed_bare_model_shows_the_current_one_as_text(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _thread(relay)

    async def down(app: str) -> list[ModelInfo]:
        raise RelayError("OpenCode is down")

    monkeypatch.setattr(relay.service, "models", down)
    await relay.say("/model", parent_id=session.webex_thread_id)
    assert relay.bot_texts()[-1] == "Model: `m1` (OpenCode is down)"
    assert not relay.webex.bot_messages()[-1].attachments


async def test_an_app_without_model_selection_gets_no_picker(relay: Relay) -> None:
    session = await relay.registry.create_session(
        SessionRecord(session_key="cursor_cdp:x", harness=Harness.CURSOR_CDP, harness_id="x", workspace="/w")
    )
    relay.router.add_adapter(CrashingAdapter())
    await relay.frontend.renderer.thread_for(session)
    stored = await relay.service.session(session.session_key)
    await relay.say("/model", parent_id=stored.webex_thread_id)
    assert "does not support model selection" in relay.bot_texts()[-1]
