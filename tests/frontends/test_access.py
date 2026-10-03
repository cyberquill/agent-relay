"""AC-10: messages from other people, group rooms and other people's card submissions are ignored
and logged."""

from __future__ import annotations

import logging

import pytest

from agent_relay.frontends.webex.access import PERSON_KEY
from tests.frontends.conftest import Relay


async def _handle(relay: Relay, message_id: str) -> None:
    await relay.frontend.handle_message(await relay.frontend.api.get_message(message_id))


async def test_first_contact_pins_person_id(relay: Relay) -> None:
    await relay.registry.kv_delete(PERSON_KEY)
    await _handle(relay, relay.webex.user_says("/help", email="ME@example.com"))
    assert await relay.registry.kv_get(PERSON_KEY) == relay.webex.user_id
    assert "agent-relay commands" in relay.bot_texts()[-1]


async def test_wrong_email_before_pinning_is_ignored(relay: Relay, caplog: pytest.LogCaptureFixture) -> None:
    await relay.registry.kv_delete(PERSON_KEY)
    caplog.set_level(logging.WARNING)
    await _handle(relay, relay.webex.user_says("/help", person="intruder", email="evil@example.com"))
    assert relay.webex.bot_messages() == []
    assert await relay.registry.kv_get(PERSON_KEY) is None
    assert "other than allowed_email" in caplog.text


async def test_other_person_after_pinning_is_ignored_even_with_right_email(
    relay: Relay, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    await _handle(relay, relay.webex.user_says("/panic", person="spoofer", email=relay.webex.user_email))
    assert relay.webex.bot_messages() == []
    assert "unexpected person id" in caplog.text


async def test_group_room_is_ignored_and_left(relay: Relay, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    await _handle(relay, relay.webex.user_says("/help", room="room-group"))
    assert relay.webex.bot_messages() == []
    assert relay.webex.left_rooms == ["room-group"]
    assert "group room" in caplog.text


async def test_card_submission_from_other_person_is_ignored(
    relay: Relay, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    action = relay.webex.user_clicks(
        "m", {"relay": "new", "step": "app", "value": "opencode"}, person="other"
    )
    await relay.frontend.handle_action(await relay.frontend.api.get_attachment_action(action))
    assert relay.webex.bot_messages() == []
    assert "someone other than the pinned person" in caplog.text


async def test_card_submission_from_group_room_is_ignored(relay: Relay) -> None:
    action = relay.webex.user_clicks(
        "m", {"relay": "new", "step": "app", "value": "opencode"}, room="room-group"
    )
    await relay.frontend.handle_action(await relay.frontend.api.get_attachment_action(action))
    assert relay.webex.bot_messages() == []
    assert relay.webex.left_rooms == ["room-group"]


async def test_card_before_any_pinning_is_ignored(relay: Relay) -> None:
    await relay.registry.kv_delete(PERSON_KEY)
    action = relay.webex.user_clicks("m", {"relay": "new", "step": "app", "value": "opencode"})
    await relay.frontend.handle_action(await relay.frontend.api.get_attachment_action(action))
    assert relay.webex.bot_messages() == []


async def test_own_messages_are_ignored(relay: Relay) -> None:
    await relay.frontend.renderer.post_top("hello from the bot")
    before = len(relay.webex.bot_messages())
    own = relay.webex.bot_messages()[-1]
    await _handle(relay, own.id)
    assert len(relay.webex.bot_messages()) == before


async def test_duplicate_delivery_is_processed_once(relay: Relay) -> None:
    message_id = relay.webex.user_says("/help")
    await _handle(relay, message_id)
    await _handle(relay, message_id)
    assert len(relay.webex.bot_messages()) == 1
