"""Bare `/away` at the top level: a scope picker with each workspace's current state, then On or Off."""

from __future__ import annotations

from pathlib import Path

from agent_relay.core import away
from agent_relay.frontends.webex import cards
from agent_relay.models.config import Workspace, WorkspacesConfig
from agent_relay.settings import load_workspaces, save_workspaces
from tests.frontends.card_tools import choice_titles, choice_values, input_by_id
from tests.frontends.conftest import Relay


def _two_workspaces(tmp_path: Path) -> str:
    other = tmp_path / "other"
    other.mkdir()
    current = load_workspaces()
    save_workspaces(
        WorkspacesConfig(workspaces=[*current.workspaces, Workspace(name="other", path=str(other))])
    )
    return str(other)


async def test_bare_away_at_the_top_is_a_picker_with_each_scopes_state(relay: Relay, tmp_path: Path) -> None:
    other = _two_workspaces(tmp_path)
    await relay.say("/away")
    msg, card = relay.webex.last_card()
    assert msg.markdown == "Away mode"
    picker = input_by_id(card, "scope")
    assert picker["style"] == "compact" and picker["value"] == cards.ALL_WORKSPACES
    assert choice_values(picker) == [cards.ALL_WORKSPACES, "proj", "other"]
    assert choice_titles(picker) == ["All workspaces · at desk", "proj · at desk", "other · at desk"]
    assert not await away.is_away(relay.registry, other)


async def test_on_and_off_apply_to_the_chosen_workspace(relay: Relay, tmp_path: Path) -> None:
    other = _two_workspaces(tmp_path)
    await relay.say("/away")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "On", scope="other")
    assert await away.is_away(relay.registry, other)
    assert not await away.is_away(relay.registry, load_workspaces().workspaces[0].path)
    assert relay.bot_texts()[-1] == f"Away mode on for `{other}`."
    await relay.say("/away")
    msg, card = relay.webex.last_card()
    assert choice_titles(input_by_id(card, "scope"))[2] == "other · 🌙 away"
    await relay.tap(msg.id, card, "Off", scope="other")
    assert not await away.is_away(relay.registry, other)


async def test_on_and_off_for_all_workspaces(relay: Relay) -> None:
    await relay.say("/away")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "On")
    assert await away.is_away(relay.registry, "/anything")
    assert relay.bot_texts()[-1] == "Away mode on for all workspaces."
    await relay.say("/away")
    msg, card = relay.webex.last_card()
    assert choice_titles(input_by_id(card, "scope"))[:2] == ["All workspaces · 🌙 away", "proj · 🌙 away"]
    await relay.tap(msg.id, card, "Off")
    assert not await away.is_away(relay.registry, "/anything")


async def test_an_unknown_scope_or_op_is_refused_and_the_card_stays_usable(relay: Relay) -> None:
    await relay.say("/away")
    msg, card = relay.webex.last_card()
    await relay.tap(msg.id, card, "On", scope="elsewhere")
    assert "Unknown workspace `elsewhere`" in relay.bot_texts()[-1]
    await relay.tap(msg.id, card, "On", scope="")
    assert relay.bot_texts()[-1] == "⚠️ Pick all workspaces or one first."
    await relay.tap(msg.id, card, "On", op="explode")
    assert "not an action this card offers" in relay.bot_texts()[-1]
    assert not await away.is_away(relay.registry, "/anything")
    await relay.tap(msg.id, card, "On", scope="proj")
    assert await away.is_away(relay.registry, load_workspaces().workspaces[0].path)


async def test_the_text_forms_and_the_thread_meaning_are_unchanged(relay: Relay) -> None:
    await relay.say("/away on")
    assert relay.bot_texts()[-1] == "Away mode on for all workspaces."
    assert not relay.webex.bot_messages()[-1].attachments
    await relay.say("/away off")
    assert relay.bot_texts()[-1] == "Away mode off for all workspaces."
    await relay.say('/new opencode proj m1 "go"')
    session = (await relay.service.sessions())[0]
    await relay.say("/away", parent_id=session.webex_thread_id)
    assert relay.bot_texts()[-1].startswith("Away mode on for `")
    assert await away.is_away(relay.registry, session.workspace)
    assert not any(m.attachments for m in relay.webex.bot_messages() if m.parent_id)
