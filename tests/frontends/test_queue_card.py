"""`/queue` as a card in a session thread: the mode, what is waiting, and the buttons that change them."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends import card_state
from agent_relay.frontends.webex.cards.queue import MAX_ITEMS
from tests.frontends.card_tools import actions_of, choice_values, input_by_id
from tests.frontends.conftest import Relay

KEY = "opencode:fake1"


async def _queue_card(relay: Relay, *waiting: str) -> tuple[str, str | None, dict[str, JsonValue]]:
    await relay.say('/new opencode proj m1 "go"')
    session = await relay.service.session(KEY)
    for text in waiting:
        await relay.registry.enqueue(KEY, "prompt", text)
    await relay.say("/queue", parent_id=session.webex_thread_id)
    message, card = relay.webex.last_card()
    return message.id, session.webex_thread_id, card


def _removes(card: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    found: list[dict[str, JsonValue]] = []
    for action in actions_of(card):
        data = action.get("data")
        if action.get("title") == "Remove" and isinstance(data, dict):
            found.append(dict(data))
    return found


async def test_the_card_shows_the_mode_and_each_waiting_message(relay: Relay) -> None:
    _, thread, card = await _queue_card(relay, "first thing", "second thing")
    message, _ = relay.webex.last_card()
    assert message.markdown == "Queue mode: queue · 2 waiting" and message.parent_id == thread
    picker = input_by_id(card, "mode")
    assert choice_values(picker) == ["queue", "steer", "interrupt"] and picker["value"] == "queue"
    assert "first thing" in str(card) and "second thing" in str(card)
    assert len(_removes(card)) == 2
    assert [a["title"] for a in actions_of(card)].count("Clear all") == 1


async def test_an_empty_queue_has_no_clear_button(relay: Relay) -> None:
    _, _, card = await _queue_card(relay)
    assert "Nothing is waiting." in str(card)
    assert "Clear all" not in [a["title"] for a in actions_of(card)]


async def test_a_long_queue_lists_ten_and_counts_the_rest(relay: Relay) -> None:
    _, _, card = await _queue_card(relay, *[f"m{i}" for i in range(MAX_ITEMS + 3)])
    assert len(_removes(card)) == MAX_ITEMS and "and 3 more" in str(card)


async def test_set_mode_changes_it_and_the_card_can_change_it_again(relay: Relay) -> None:
    msg, thread, card = await _queue_card(relay)
    await relay.tap(msg, card, "Set mode", mode="steer")
    assert await relay.service.queue_mode(KEY) == "steer"
    assert relay.bot_texts()[-1].startswith("Queue mode: **steer**")
    assert relay.webex.bot_messages()[-1].parent_id == thread
    await relay.tap(msg, card, "Set mode", mode="interrupt")
    assert await relay.service.queue_mode(KEY) == "interrupt"


async def test_set_mode_refuses_something_the_card_never_offered(relay: Relay) -> None:
    msg, _, card = await _queue_card(relay)
    await relay.tap(msg, card, "Set mode", mode="shout")
    assert "Unknown queue mode `shout`" in relay.bot_texts()[-1]
    assert await relay.service.queue_mode(KEY) == "queue"
    await relay.tap(msg, card, "Set mode", mode="steer")
    assert await relay.service.queue_mode(KEY) == "steer"


async def test_remove_drops_one_message_and_a_repeat_is_harmless(relay: Relay) -> None:
    msg, _, card = await _queue_card(relay, "keep", "drop")
    first, second = _removes(card)
    await relay.click(msg, second)
    assert [q.text for q in await relay.service.queued(KEY)] == ["keep"]
    assert relay.bot_texts()[-1] == "Removed."
    await relay.click(msg, second)
    assert relay.bot_texts()[-1] == "That message was already sent or removed."
    await relay.click(msg, first)
    assert await relay.service.queued(KEY) == []


async def test_remove_refuses_an_id_that_is_not_a_number(relay: Relay) -> None:
    msg, _, card = await _queue_card(relay, "x")
    [only] = _removes(card)
    await relay.click(msg, {**only, "id": "1"})
    assert "not a message this card listed" in relay.bot_texts()[-1]
    assert len(await relay.service.queued(KEY)) == 1


async def test_clear_all_empties_the_queue(relay: Relay) -> None:
    msg, _, card = await _queue_card(relay, "a", "b")
    await relay.tap(msg, card, "Clear all")
    assert relay.bot_texts()[-1] == "Cleared 2 messages."
    assert await relay.service.queued(KEY) == []
    await relay.tap(msg, card, "Clear all")
    assert relay.bot_texts()[-1] == "Nothing was queued."


async def test_an_expired_queue_card_is_refused(relay: Relay) -> None:
    msg, _, card = await _queue_card(relay, "a")
    [only] = _removes(card)
    await relay.registry.kv_delete(f"{card_state.NONCE_PREFIX}{only['nonce']}")
    await relay.click(msg, only)
    assert relay.bot_texts()[-1] == card_state.EXPIRED
    assert len(await relay.service.queued(KEY)) == 1
