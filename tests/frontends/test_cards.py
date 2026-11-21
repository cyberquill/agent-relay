"""AC-12: Adaptive Cards v1.3 with Input.ChoiceSet and Action.Submit, one card per message,
a one-shot card deleted once it is done. Plus the rules every card follows: a one-use nonce,
server-side re-validation, the size limit, and a text line that says what the card is about."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue

from agent_relay.core.service.common import DeliveryError
from agent_relay.frontends import card_state
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.cards.common import card as build_card
from agent_relay.models.events import (
    Harness,
    Origin,
    QuestionAnswered,
    QuestionAsked,
    ResolvedBy,
)
from agent_relay.models.sessions import PendingRequest, SessionRecord
from tests.frontends.conftest import Relay, submit_data

H = Harness.OPENCODE
ALLOWED_ELEMENTS = {"TextBlock", "Input.ChoiceSet", "Input.Text", "Input.Toggle", "ActionSet"}


def _choices(block: JsonValue) -> list[JsonValue]:
    assert isinstance(block, dict) and isinstance(block["choices"], list)
    return block["choices"]


def _walk(card: dict[str, JsonValue]) -> None:
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.3"
    body = card["body"]
    actions = card["actions"]
    assert isinstance(body, list) and isinstance(actions, list)
    sets = [b for b in body if isinstance(b, dict) and b["type"] == "ActionSet"]
    assert actions or sets
    for block in body:
        assert isinstance(block, dict) and block["type"] in ALLOWED_ELEMENTS
        if block["type"] == "Input.ChoiceSet":
            choices = block["choices"]
            assert isinstance(choices, list) and len(choices) <= cards.MAX_CHOICES
            assert all(isinstance(c, dict) and {"title", "value"} <= c.keys() for c in choices)
        if block["type"] == "ActionSet":
            assert isinstance(block["actions"], list) and block["actions"]
            actions = [*actions, *block["actions"]]
    for action in actions:
        assert isinstance(action, dict)
        if action["type"] == "Action.ShowCard":
            inner = action["card"]
            assert isinstance(inner, dict)
            _walk(inner)
            continue
        assert action["type"] == "Action.Submit"
        data = action["data"]
        assert isinstance(data, dict) and isinstance(data["relay"], str)
        assert isinstance(data["nonce"], str) and data["nonce"]


def test_builders_produce_valid_v13_cards() -> None:
    _walk(cards.question("q1", "label", "Which?", ("a", "b"), nonce="n"))
    _walk(cards.question("q2", "label", "Free text?", (), nonce="n"))
    ws = [("proj", "proj")]
    agents = [("opencode:default", "🟩 OpenCode · Default"), ("opencode:m1", "🟩 OpenCode · M1")]
    _walk(cards.new_session(ws, "proj", agents, "opencode:default", None, nonce="n"))
    _walk(
        cards.new_session(
            ws, "proj", agents, "opencode:m1", "x", nonce="n", unavailable=["Cursor CLI (down)"], files=2
        )
    )
    _walk(cards.model_switch("s", "m1", [("m1", "M1")], "m1", nonce="n"))
    _walk(cards.panic_confirm(nonce="n"))


def test_fit_card_shrinks_then_gives_up() -> None:
    big_choices = build_card(
        [
            {
                "type": "Input.ChoiceSet",
                "id": "c",
                "choices": [{"title": "t", "value": str(i)} for i in range(5000)],
            }
        ],
        [],
    )
    fitted = cards.fit_card("hi", big_choices)
    assert fitted is not None and cards.message_bytes("hi", fitted) <= cards.CARD_LIMIT_BYTES
    blocks: list[JsonValue] = [{"type": "TextBlock", "text": "x" * 2000} for _ in range(40)]
    assert cards.fit_card("hi", build_card(blocks, [])) is None


async def _pending(relay: Relay, request_id: str, minutes: int = 10) -> SessionRecord:
    session = await relay.registry.get_session("opencode:c1") or await relay.registry.create_session(
        SessionRecord(
            session_key="opencode:c1", harness=H, harness_id="c1", workspace="/w/p", origin=Origin.PHONE
        )
    )
    await relay.registry.add_pending(
        PendingRequest(
            request_id=request_id,
            kind="question",
            session_key=session.session_key,
            payload={"source": "adapter"},
            deadline=datetime.now(UTC) + timedelta(minutes=minutes),
        )
    )
    return session


async def _ask_question(
    relay: Relay, request_id: str = "q1", options: tuple[str, ...] = ("pg", "sqlite")
) -> SessionRecord:
    session = await _pending(relay, request_id)
    await relay.frontend.renderer.handle(
        QuestionAsked(
            harness=H,
            session_key=session.session_key,
            request_id=request_id,
            question="Which db?",
            options=options,
        )
    )
    return session


async def test_a_card_can_be_answered_once_only(relay: Relay) -> None:
    await _ask_question(relay)
    card_msg, card = relay.webex.last_card()
    await relay.press(card_msg.id, card, "Answer", choice="pg", custom="")
    await relay.press(card_msg.id, card, "Answer", choice="sqlite", custom="")
    assert relay.adapter.answers == [("q1", "pg")]
    assert relay.bot_texts()[-1] == card_state.EXPIRED


async def test_a_submit_without_a_valid_nonce_does_nothing(relay: Relay) -> None:
    await _ask_question(relay)
    card_msg, card = relay.webex.last_card()
    data = submit_data(card, "Answer")
    del data["nonce"]
    await relay.click(card_msg.id, {**data, "choice": "pg"})
    await relay.click(card_msg.id, {**data, "choice": "pg", "nonce": "made-up"})
    assert relay.adapter.answers == []
    assert relay.bot_texts()[-2:] == [card_state.EXPIRED, card_state.EXPIRED]
    await relay.press(card_msg.id, card, "Answer", choice="pg", custom="")
    assert relay.adapter.answers == [("q1", "pg")]


async def test_a_double_tap_on_the_new_card_starts_one_session(relay: Relay) -> None:
    await relay.say("/new opencode proj m1")
    card_msg, card = relay.webex.last_card()
    await relay.tap(card_msg.id, card, "Start", prompt="go")
    await relay.tap(card_msg.id, card, "Start", prompt="go")
    assert [s.prompt for s in relay.adapter.started] == ["go"]
    assert relay.bot_texts()[-1] == card_state.EXPIRED


async def test_question_message_carries_the_question(relay: Relay) -> None:
    await _ask_question(relay)
    message, _ = relay.webex.last_card()
    assert message.markdown == "❓ Question: Which db?"


async def test_question_card_answer_and_skip(relay: Relay) -> None:
    await _ask_question(relay)
    card_msg, card = relay.webex.last_card()
    await relay.press(card_msg.id, card, "Answer", choice="pg", custom="")
    assert relay.adapter.answers == [("q1", "pg")]
    assert relay.webex.messages[card_msg.id].deleted  # deleted, not edited: it cannot be tapped again
    assert relay.webex.edits.get(card_msg.id, 0) == 0
    assert await relay.registry.kv_get(f"{card_state.OFFER_PREFIX}q1") is None


async def test_question_custom_text_wins_and_skip_sends_none(relay: Relay) -> None:
    await _ask_question(relay, "q2")
    first_msg, first = relay.webex.last_card()
    await _ask_question(relay, "q3")
    second_msg, second = relay.webex.last_card()
    await relay.press(first_msg.id, first, "Answer", choice="pg", custom="mysql")
    await relay.press(second_msg.id, second, "Skip")
    assert relay.adapter.answers == [("q2", "mysql"), ("q3", None)]


async def test_a_choice_the_card_never_offered_is_refused_and_the_card_stays_usable(relay: Relay) -> None:
    await _ask_question(relay)
    card_msg, card = relay.webex.last_card()
    await relay.press(card_msg.id, card, "Answer", choice="oracle", custom="")
    assert relay.adapter.answers == []
    assert relay.bot_texts()[-1] == "⚠️ That choice was not one of the options offered."
    await relay.press(card_msg.id, card, "Answer", choice="sqlite", custom="")
    assert relay.adapter.answers == [("q1", "sqlite")]


async def test_an_over_long_answer_and_an_unknown_action_are_refused(relay: Relay) -> None:
    await _ask_question(relay, options=())
    card_msg, card = relay.webex.last_card()
    await relay.press(card_msg.id, card, "Answer", custom="x" * (cards.MAX_ANSWER_CHARS + 1))
    assert "too long" in relay.bot_texts()[-1]
    await relay.press(card_msg.id, card, "Answer", op="explode")
    assert "not an action this card offers" in relay.bot_texts()[-1]
    assert relay.adapter.answers == []


async def test_a_relay_error_from_a_submit_is_shown_in_the_thread_and_can_be_retried(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _ask_question(relay)
    card_msg, card = relay.webex.last_card()
    real = relay.service.answer_question
    failures = [DeliveryError("OpenCode could not be reached; the request is still open.")]

    async def flaky(request_id: str, answer: str | None, by: ResolvedBy = "phone") -> bool:
        if failures:
            raise failures.pop()
        return await real(request_id, answer, by)

    monkeypatch.setattr(relay.service, "answer_question", flaky)
    await relay.press(card_msg.id, card, "Answer", choice="pg", custom="")
    last = relay.webex.bot_messages()[-1]
    assert last.markdown == "⚠️ OpenCode could not be reached; the request is still open."
    assert last.parent_id == (await relay.service.session(session.session_key)).webex_thread_id
    assert not relay.webex.messages[card_msg.id].deleted  # a failed tap leaves the card to try again
    await relay.press(card_msg.id, card, "Answer", choice="pg", custom="")
    assert relay.adapter.answers == [("q1", "pg")]
    assert relay.webex.messages[card_msg.id].deleted


async def test_the_card_of_a_question_whose_session_ended_is_deleted(relay: Relay) -> None:
    session = await _ask_question(relay)
    question_msg, _ = relay.webex.last_card()
    await relay.frontend.renderer.handle(
        QuestionAnswered(harness=H, session_key=session.session_key, request_id="q1", answer=None, by="ended")
    )
    assert relay.webex.messages[question_msg.id].deleted


async def test_a_card_too_big_for_webex_goes_as_text(relay: Relay) -> None:
    blocks: list[JsonValue] = [{"type": "TextBlock", "text": "y" * 2_000} for _ in range(40)]
    card = build_card(blocks, [{"type": "Action.Submit", "title": "Go", "data": {"relay": "new"}}])
    await relay.frontend.renderer.post_top("Pick one", card=card)
    message = relay.webex.bot_messages()[-1]
    assert message.attachments == [] and "too large" in message.markdown
    assert relay.webex.violations == []


async def test_a_big_card_that_shrinks_is_sent_minified(relay: Relay) -> None:
    choices: list[JsonValue] = [{"title": f"Model number {i}", "value": f"m{i}"} for i in range(900)]
    card = build_card([{"type": "Input.ChoiceSet", "id": "c", "choices": choices}], [])
    await relay.frontend.renderer.post_top("Pick one", card=card)
    sent = relay.webex.last_card()[1]
    body = sent["body"]
    assert isinstance(body, list)
    assert len(_choices(body[0])) == cards.MAX_CHOICES
    assert relay.webex.violations == []
