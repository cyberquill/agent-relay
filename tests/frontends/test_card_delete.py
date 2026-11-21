"""A one-shot card is deleted once its action succeeded; a failed tap or a reusable card stays."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.core.service import RelayError, RelayService
from agent_relay.frontends.actions import Outcome, register_action
from agent_relay.frontends.webex.handlers import Handlers
from tests.frontends.conftest import Relay

ONE_SHOT = "test-one-shot"
REUSABLE = "test-reusable"


async def _act(service: RelayService, handlers: Handlers, inputs: dict[str, JsonValue]) -> Outcome:
    if inputs.get("fail"):
        raise RelayError("not this time")
    if inputs.get("stale"):
        return "already_answered"
    return "done"


register_action(ONE_SHOT, _act)
register_action(REUSABLE, _act, reusable=True)


async def _card(relay: Relay) -> tuple[str, str]:
    posted = await relay.frontend.renderer.post_top("a card")
    return posted.id, await relay.nonce()


async def test_a_one_shot_card_is_deleted_after_it_succeeds(relay: Relay) -> None:
    message_id, nonce = await _card(relay)
    await relay.click(message_id, {"relay": ONE_SHOT, "nonce": nonce})
    assert relay.webex.messages[message_id].deleted


async def test_a_failed_tap_leaves_the_card_to_be_tapped_again(relay: Relay) -> None:
    message_id, nonce = await _card(relay)
    await relay.click(message_id, {"relay": ONE_SHOT, "nonce": nonce, "fail": True})
    assert not relay.webex.messages[message_id].deleted
    assert relay.bot_texts()[-1] == "⚠️ not this time"
    await relay.click(message_id, {"relay": ONE_SHOT, "nonce": nonce})
    assert relay.webex.messages[message_id].deleted


async def test_an_expired_tap_does_not_delete_the_card(relay: Relay) -> None:
    message_id, _ = await _card(relay)
    await relay.click(message_id, {"relay": ONE_SHOT, "nonce": "never-issued"})
    assert not relay.webex.messages[message_id].deleted


async def test_a_reusable_card_stays_after_it_succeeds(relay: Relay) -> None:
    message_id, nonce = await _card(relay)
    await relay.click(message_id, {"relay": REUSABLE, "nonce": nonce})
    await relay.click(message_id, {"relay": REUSABLE, "nonce": nonce})
    assert not relay.webex.messages[message_id].deleted


async def test_a_card_that_cannot_be_deleted_does_not_fail_the_tap(relay: Relay) -> None:
    message_id, nonce = await _card(relay)
    relay.webex.refuse_delete = True
    await relay.click(message_id, {"relay": ONE_SHOT, "nonce": nonce})
    assert not relay.webex.messages[message_id].deleted
