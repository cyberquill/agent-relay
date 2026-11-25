"""D146: `/chats` lists desk chats on a card, and Continue or `/takeover N` opens a thread on one."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue

from agent_relay import paths
from agent_relay.core import away
from agent_relay.frontends.webex.commands.chats import parse_filters
from agent_relay.settings import load_workspaces
from tests.core import chat_stores as stores
from tests.frontends.conftest import Relay, submit_data


def proj(relay: Relay) -> Path:
    return Path(load_workspaces().workspaces[0].path).resolve()


def texts(card: dict[str, JsonValue]) -> str:
    return str(card)


# filters


def test_a_span_an_app_and_a_workspace_are_told_apart_from_free_text() -> None:
    days, apps, workspace, words = parse_filters(("2w", "copilot", "proj", "parser"), frozenset({"proj"}))
    assert days == 14 and apps is not None and workspace == "proj" and words == ("parser",)


def test_days_default_to_a_week_and_are_capped() -> None:
    assert parse_filters((), frozenset())[0] == 7
    with pytest.raises(Exception, match="1 to 90 days"):
        parse_filters(("200d",), frozenset())


# the card


async def test_chats_with_none_says_so_and_how_to_look_further(relay: Relay) -> None:
    await relay.say("/chats")
    assert "No desk chats in the last 7 day(s)" in relay.bot_texts()[-1]
    assert "/chats 14d" in relay.bot_texts()[-1]


async def test_chats_with_only_unregistered_folders_counts_them(relay: Relay, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    stores.opencode_db(other, "ses_x")
    await relay.say("/chats")
    assert "1 in folders that are not workspaces" in relay.bot_texts()[-1]


async def test_chats_sends_one_card_with_a_continue_button_per_chat(relay: Relay) -> None:
    stores.opencode_db(proj(relay), "ses_1", prompt="Send the report", reply="Sent.", seconds_old=600)
    stores.cursor_chat(proj(relay), "c1", seconds_old=7200)
    await relay.say("/chats")
    _, card = relay.webex.last_card()
    body = texts(card)
    assert "Send the report" in body and "Add a parser test" in body and "Sent." in body
    assert body.count("Continue") == 2
    first, second = (a for a in _actions(card))
    assert first["pick"] == "1" and second["pick"] == "2" and first["nonce"] == second["nonce"]
    assert first["relay"] == "chats"


def _actions(card: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    found: list[dict[str, JsonValue]] = []

    def walk(node: JsonValue) -> None:
        if isinstance(node, dict):
            if node.get("type") == "Action.Submit" and isinstance(node.get("data"), dict):
                found.append(dict(node["data"]))  # type: ignore[arg-type]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return found


async def test_the_card_hides_a_chat_that_is_older_than_the_window_until_asked_for(relay: Relay) -> None:
    stores.opencode_db(proj(relay), "ses_old", seconds_old=9 * 86400)
    await relay.say("/chats")
    assert "No desk chats" in relay.bot_texts()[-1]
    await relay.say("/chats 14d")
    assert relay.webex.last_card()[1]


async def test_a_secret_in_a_chat_is_not_on_the_card(relay: Relay) -> None:
    stores.opencode_db(proj(relay), "ses_1", reply="key ghp_" + "b" * 36)
    await relay.say("/chats")
    assert "ghp_" not in texts(relay.webex.last_card()[1])


# taking over


async def test_continue_opens_a_thread_with_the_recap_and_marks_the_session(relay: Relay) -> None:
    stores.opencode_db(proj(relay), "ses_1", prompt="Send the report", reply="Sent.")
    await relay.say("/chats")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Continue")
    recap = relay.thread_texts()
    assert recap and "Picked up from the desk" in recap[0] and "Send the report" in recap[0]
    [session] = await relay.registry.list_sessions()
    assert away.taken_over(session) is False  # a handoff is a new phone session, not a taken-over desk one
    assert session.title is not None and session.title.startswith("↪ ")
    assert "**new** session" in recap[0]


async def test_continue_on_a_running_cursor_chat_arms_it_and_says_it_will_hold(relay: Relay) -> None:
    stores.cursor_chat(proj(relay), "c1", finished=False, seconds_old=5)
    await relay.say("/chats")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Continue")
    [session] = await relay.registry.list_sessions()
    assert session.session_key == "cursor:c1" and away.armed(session)
    assert any("holds" in t for t in relay.thread_texts())


async def test_takeover_n_uses_the_last_list(relay: Relay) -> None:
    stores.cursor_chat(proj(relay), "c1", finished=False, seconds_old=5)
    await relay.say("/chats")
    await relay.say("/takeover 1")
    [session] = await relay.registry.list_sessions()
    assert away.armed(session)


async def test_continue_is_an_alias_of_takeover(relay: Relay) -> None:
    stores.cursor_chat(proj(relay), "c1", finished=False, seconds_old=5)
    await relay.say("/chats")
    await relay.say("/continue 1")
    assert len(await relay.registry.list_sessions()) == 1


async def test_takeover_before_any_list_says_to_send_chats_first(relay: Relay) -> None:
    await relay.say("/takeover 1")
    assert "Send `/chats` first" in relay.bot_texts()[-1]


async def test_takeover_with_a_number_that_is_not_listed_is_refused(relay: Relay) -> None:
    stores.cursor_chat(proj(relay), "c1", finished=False, seconds_old=5)
    await relay.say("/chats")
    await relay.say("/takeover 7")
    assert "not one of the chats listed" in relay.bot_texts()[-1]
    assert await relay.registry.list_sessions() == []


async def test_takeover_without_a_number_explains(relay: Relay) -> None:
    await relay.say("/takeover two")
    assert "Give the number from the list" in relay.bot_texts()[-1]


async def test_a_chat_that_finished_being_listed_is_reported_not_guessed(relay: Relay) -> None:
    stores.opencode_db(proj(relay), "ses_1")
    await relay.say("/chats")
    msg, card = relay.webex.last_card()
    (paths.opencode_db()).unlink()
    await relay.press(msg.id, card, "Continue")
    assert any("not in the list any more" in t for t in relay.bot_texts())


async def test_a_running_chat_in_an_app_without_hooks_is_refused_on_the_card(relay: Relay) -> None:
    stores.opencode_db(proj(relay), "ses_1", finished=False, seconds_old=3)
    await relay.say("/chats")
    msg, card = relay.webex.last_card()
    assert "running" in texts(card)
    await relay.press(msg.id, card, "Continue")
    assert any("running at the desk now" in t for t in relay.bot_texts())
    assert await relay.registry.list_sessions() == []


async def test_the_continue_button_works_once(relay: Relay) -> None:
    stores.cursor_chat(proj(relay), "c1", finished=False, seconds_old=5)
    await relay.say("/chats")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Continue")
    await relay.press(msg.id, card, "Continue")
    assert relay.bot_texts()[-1] == "This card has expired or was already answered."
    assert len(await relay.registry.list_sessions()) == 1


async def test_a_tap_with_a_made_up_nonce_is_rejected(relay: Relay) -> None:
    stores.cursor_chat(proj(relay), "c1", finished=False, seconds_old=5)
    await relay.say("/chats")
    msg, card = relay.webex.last_card()
    data = submit_data(card, "Continue")
    await relay.click(msg.id, {**data, "nonce": "forged"})
    assert relay.bot_texts()[-1] == "This card has expired or was already answered."
    assert await relay.registry.list_sessions() == []
