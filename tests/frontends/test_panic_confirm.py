"""`/panic` asks first; the confirmation is good for 60 seconds; `/panic now` does not ask."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent_relay import paths
from agent_relay.core import away
from agent_relay.frontends import card_state
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.commands.away import PANIC_ISSUED_PREFIX
from agent_relay.models.events import utcnow
from agent_relay.models.sessions import SessionRecord
from tests.frontends.card_tools import actions_of
from tests.frontends.conftest import Relay, submit_data
from tests.frontends.test_dispatch import Gated, _arrive, until


async def _running(relay: Relay) -> SessionRecord:
    await relay.say('/new opencode proj m1 "start"')
    await relay.say("/away on")
    return (await relay.service.sessions())[0]


async def test_panic_sends_a_confirmation_and_does_nothing_yet(relay: Relay) -> None:
    await _running(relay)
    await relay.say("/panic")
    msg, card = relay.webex.last_card()
    assert msg.markdown == "Cancel everything?"
    assert [a["title"] for a in actions_of(card)] == ["Cancel everything", "Cancel"]
    assert relay.adapter.cancelled == [] and not paths.pause_flag().exists()
    assert "60 seconds" in str(card) and cards.CONFIRM_WINDOW_S == 60


async def test_confirming_cancels_everything_and_pauses(relay: Relay) -> None:
    session = await _running(relay)
    await relay.say("/panic")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Cancel everything")
    assert relay.adapter.cancelled == [session.session_key]
    assert paths.pause_flag().exists()
    assert not await away.is_away(relay.registry, session.workspace)
    assert "Paused" in relay.bot_texts()[-1]


async def test_cancel_leaves_everything_as_it_is(relay: Relay) -> None:
    await _running(relay)
    await relay.say("/panic")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Cancel")
    assert relay.bot_texts()[-1] == "Left everything as it is."
    assert relay.adapter.cancelled == [] and not paths.pause_flag().exists()


async def test_a_confirmation_older_than_sixty_seconds_is_refused(relay: Relay) -> None:
    await _running(relay)
    await relay.say("/panic")
    msg, card = relay.webex.last_card()
    key = f"{PANIC_ISSUED_PREFIX}{submit_data(card, 'Cancel everything')['nonce']}"
    await relay.registry.kv_set(key, (utcnow() - timedelta(seconds=61)).isoformat())
    await relay.press(msg.id, card, "Cancel everything")
    assert "expired" in relay.bot_texts()[-1] and "/panic" in relay.bot_texts()[-1]
    assert relay.adapter.cancelled == [] and not paths.pause_flag().exists()


async def test_a_confirmation_just_inside_the_window_still_works(relay: Relay) -> None:
    await _running(relay)
    await relay.say("/panic")
    msg, card = relay.webex.last_card()
    key = f"{PANIC_ISSUED_PREFIX}{submit_data(card, 'Cancel everything')['nonce']}"
    await relay.registry.kv_set(key, (utcnow() - timedelta(seconds=55)).isoformat())
    await relay.press(msg.id, card, "Cancel everything")
    assert paths.pause_flag().exists()


async def test_a_confirmation_with_no_recorded_time_is_refused(relay: Relay) -> None:
    await _running(relay)
    await relay.say("/panic")
    msg, card = relay.webex.last_card()
    await relay.registry.kv_delete(f"{PANIC_ISSUED_PREFIX}{submit_data(card, 'Cancel everything')['nonce']}")
    await relay.press(msg.id, card, "Cancel everything")
    assert "expired" in relay.bot_texts()[-1]
    assert relay.adapter.cancelled == []


async def test_the_confirmation_is_single_use(relay: Relay) -> None:
    await _running(relay)
    await relay.say("/panic")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Cancel everything")
    cancelled = list(relay.adapter.cancelled)
    await relay.press(msg.id, card, "Cancel everything")
    assert relay.adapter.cancelled == cancelled
    assert relay.bot_texts()[-1] == card_state.EXPIRED


async def test_panic_now_skips_the_card(relay: Relay) -> None:
    session = await _running(relay)
    await relay.say("/panic now")
    assert relay.adapter.cancelled == [session.session_key] and paths.pause_flag().exists()
    assert not relay.webex.bot_messages()[-1].attachments


async def test_an_unknown_op_is_refused_and_the_card_stays_usable(relay: Relay) -> None:
    await _running(relay)
    await relay.say("/panic")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Cancel everything", op="explode")
    assert "not an action this card offers" in relay.bot_texts()[-1]
    assert not paths.pause_flag().exists()
    await relay.press(msg.id, card, "Cancel everything")
    assert paths.pause_flag().exists()


async def test_the_confirmation_card_is_sent_at_once_even_when_the_top_level_is_busy(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    gated = Gated(relay, monkeypatch, {"/doctor"})
    await _arrive(relay, "/doctor")
    await until(lambda: gated.started == ["/doctor"])
    await _arrive(relay, "/panic")
    await until(lambda: any(m.markdown == "Cancel everything?" for m in relay.webex.bot_messages()))
    assert gated.done == ["/panic"]
    gated.gates["/doctor"].set()
    await relay.frontend.drain()
