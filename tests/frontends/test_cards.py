"""AC-12: Adaptive Cards v1.3 with Input.ChoiceSet and Action.Submit, one card per message,
each card edited exactly once with its outcome. Plus the rules every card follows: a one-use nonce,
server-side re-validation, the size limit, and a text line that says what the card is about."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue

from agent_relay.adapters.base import Decision
from agent_relay.core.service.common import DeliveryError
from agent_relay.frontends import card_state
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.cards.common import card as build_card
from agent_relay.models.events import (
    ApprovalRequested,
    ApprovalResolved,
    Harness,
    QuestionAnswered,
    QuestionAsked,
    ResolvedBy,
)
from agent_relay.models.sessions import PendingRequest, SessionRecord
from tests.frontends.conftest import Relay, submit_data

H = Harness.OPENCODE
ALLOWED_ELEMENTS = {"TextBlock", "Input.ChoiceSet", "Input.Text"}


def _choices(block: JsonValue) -> list[JsonValue]:
    assert isinstance(block, dict) and isinstance(block["choices"], list)
    return block["choices"]


def _walk(card: dict[str, JsonValue]) -> None:
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.3"
    body = card["body"]
    actions = card["actions"]
    assert isinstance(body, list) and isinstance(actions, list) and actions
    for block in body:
        assert isinstance(block, dict) and block["type"] in ALLOWED_ELEMENTS
        if block["type"] == "Input.ChoiceSet":
            choices = block["choices"]
            assert isinstance(choices, list) and len(choices) <= cards.MAX_CHOICES
            assert all(isinstance(c, dict) and {"title", "value"} <= c.keys() for c in choices)
    for action in actions:
        assert isinstance(action, dict) and action["type"] == "Action.Submit"
        data = action["data"]
        assert isinstance(data, dict) and isinstance(data["relay"], str)
        assert isinstance(data["nonce"], str) and data["nonce"]


def test_builders_produce_valid_v13_cards() -> None:
    _walk(cards.approval("r1", "label", "rm -rf /tmp/x", "10m", nonce="n"))
    _walk(cards.question("q1", "label", "Which?", ("a", "b"), nonce="n"))
    _walk(cards.question("q2", "label", "Free text?", (), nonce="n"))
    _walk(cards.choose("app", "Which app?", [("opencode", "OpenCode")], {"ws": "proj"}, nonce="n"))
    _walk(cards.prompt({"app": "opencode"}, nonce="n"))


def test_choose_carries_previous_answers() -> None:
    card = cards.choose("ws", "Which?", [("proj", "proj")], {"app": "opencode"}, nonce="n1")
    assert submit_data(card, "Next") == {"relay": "new", "step": "ws", "nonce": "n1", "app": "opencode"}


def test_a_long_choice_list_is_capped_and_says_so() -> None:
    choices = [(f"m{i}", f"Model {i}") for i in range(60)]
    card = cards.choose("model", "Which model?", choices, {}, nonce="n")
    _walk(card)
    body = card["body"]
    assert isinstance(body, list)
    assert len(_choices(body[1])) == cards.MAX_CHOICES
    assert "Showing 25 of 60" in cards.card_fallback({"body": [body[2]]})


def test_the_approval_detail_is_monospace() -> None:
    card = cards.approval("r1", "label", "echo `hi` *x*", "10m", nonce="n")
    body = card["body"]
    assert isinstance(body, list)
    detail = body[2]
    assert isinstance(detail, dict) and detail["fontType"] == "Monospace" and detail["wrap"] is True


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


async def _pending(relay: Relay, kind: str, request_id: str, minutes: int = 10) -> SessionRecord:
    session = await relay.registry.get_session("opencode:c1") or await relay.registry.create_session(
        SessionRecord(session_key="opencode:c1", harness=H, harness_id="c1", workspace="/w/p")
    )
    await relay.registry.add_pending(
        PendingRequest(
            request_id=request_id,
            kind="approval" if kind == "approval" else "question",
            session_key=session.session_key,
            payload={"source": "adapter"},
            deadline=datetime.now(UTC) + timedelta(minutes=minutes),
        )
    )
    return session


async def _ask_approval(relay: Relay, request_id: str = "r1", detail: str = "git push") -> SessionRecord:
    session = await _pending(relay, "approval", request_id)
    await relay.frontend.renderer.handle(
        ApprovalRequested(
            harness=H, session_key=session.session_key, request_id=request_id, tool="bash", detail=detail
        )
    )
    return session


async def _ask_question(
    relay: Relay, request_id: str = "q1", options: tuple[str, ...] = ("pg", "sqlite")
) -> SessionRecord:
    session = await _pending(relay, "question", request_id)
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


async def test_approval_card_is_edited_once_with_outcome(relay: Relay) -> None:
    await _ask_approval(relay)
    card_msg, card = relay.webex.last_card()
    _walk(card)
    assert sum(1 for m in relay.webex.bot_messages() if m.attachments) == 1
    await relay.press(card_msg.id, card, "Allow")
    assert relay.webex.edits[card_msg.id] == 1
    assert relay.webex.messages[card_msg.id].markdown == "✅ Allowed"
    assert relay.adapter.approvals == [("r1", "allow")]
    # a tap from another card for the same request: first answer wins
    await relay.click(card_msg.id, {**submit_data(card, "Deny"), "nonce": await relay.nonce()})
    assert relay.webex.edits[card_msg.id] == 1
    assert relay.adapter.approvals == [("r1", "allow")]
    assert relay.bot_texts()[-1] == "That request was already answered."


async def test_a_card_can_be_answered_once_only(relay: Relay) -> None:
    await _ask_approval(relay)
    card_msg, card = relay.webex.last_card()
    await relay.press(card_msg.id, card, "Allow")
    await relay.press(card_msg.id, card, "Deny")
    assert relay.adapter.approvals == [("r1", "allow")]
    assert relay.bot_texts()[-1] == card_state.EXPIRED


async def test_a_submit_without_a_valid_nonce_does_nothing(relay: Relay) -> None:
    await _ask_approval(relay)
    card_msg, card = relay.webex.last_card()
    data = submit_data(card, "Allow")
    del data["nonce"]
    await relay.click(card_msg.id, data)
    await relay.click(card_msg.id, {**data, "nonce": "made-up"})
    assert relay.adapter.approvals == []
    assert relay.bot_texts()[-2:] == [card_state.EXPIRED, card_state.EXPIRED]
    await relay.press(card_msg.id, card, "Allow")
    assert relay.adapter.approvals == [("r1", "allow")]


async def test_a_double_tap_on_the_wizard_starts_one_session(relay: Relay) -> None:
    await relay.say("/new opencode proj m1")
    card_msg, card = relay.webex.last_card()
    await relay.press(card_msg.id, card, "Start", value="go")
    await relay.press(card_msg.id, card, "Start", value="go")
    assert [s.prompt for s in relay.adapter.started] == ["go"]
    assert relay.bot_texts()[-1] == card_state.EXPIRED


async def test_approval_message_names_the_tool_and_the_deadline(relay: Relay) -> None:
    await _ask_approval(relay, detail="rm -rf /tmp/x")
    message, card = relay.webex.last_card()
    assert message.markdown == "🔐 Approval needed: bash · auto-deny in 10m"
    body = card["body"]
    assert isinstance(body, list) and all(isinstance(b, dict) for b in body)
    texts = [str(b["text"]) for b in body if isinstance(b, dict)]
    assert texts[-1] == "Auto-deny in 10m."
    detail = body[2]
    assert (
        isinstance(detail, dict) and detail["text"] == "rm -rf /tmp/x" and detail["fontType"] == "Monospace"
    )


async def test_the_deadline_is_the_pending_requests_not_the_configured_one(relay: Relay) -> None:
    session = await _pending(relay, "approval", "r9", minutes=95)
    await relay.frontend.renderer.handle(
        ApprovalRequested(harness=H, session_key=session.session_key, request_id="r9", tool="bash")
    )
    message, _ = relay.webex.last_card()
    assert message.markdown.endswith("auto-deny in 1h 35m")


async def test_the_approval_detail_keeps_head_and_tail(relay: Relay) -> None:
    detail = "echo start; " + "x" * 5000 + "; rm -rf ~"
    await _ask_approval(relay, detail=detail)
    _, card = relay.webex.last_card()
    body = card["body"]
    assert isinstance(body, list) and isinstance(body[2], dict)
    shown = str(body[2]["text"])
    assert shown.startswith("echo start;") and shown.endswith("; rm -rf ~")
    assert "more chars" in shown and len(shown) < 1_600


async def test_question_message_carries_the_question(relay: Relay) -> None:
    await _ask_question(relay)
    message, _ = relay.webex.last_card()
    assert message.markdown == "❓ Question: Which db?"


async def test_question_card_answer_and_skip(relay: Relay) -> None:
    await _ask_question(relay)
    card_msg, card = relay.webex.last_card()
    await relay.press(card_msg.id, card, "Answer", choice="pg", custom="")
    assert relay.adapter.answers == [("q1", "pg")]
    assert relay.webex.messages[card_msg.id].markdown == "💬 Answered: pg"
    assert relay.webex.edits[card_msg.id] == 1
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


async def test_an_unknown_decision_is_refused(relay: Relay) -> None:
    await _ask_approval(relay)
    card_msg, card = relay.webex.last_card()
    await relay.press(card_msg.id, card, "Allow", decision="always")
    assert relay.adapter.approvals == []
    assert relay.bot_texts()[-1] == "⚠️ That is not a decision this card offers."


async def test_a_relay_error_from_a_submit_is_shown_in_the_thread_and_can_be_retried(
    relay: Relay, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _ask_approval(relay)
    card_msg, card = relay.webex.last_card()
    real = relay.service.answer_approval
    failures = [DeliveryError("OpenCode could not be reached; the request is still open.")]

    async def flaky(request_id: str, decision: Decision, by: ResolvedBy = "phone") -> bool:
        if failures:
            raise failures.pop()
        return await real(request_id, decision, by)

    monkeypatch.setattr(relay.service, "answer_approval", flaky)
    await relay.press(card_msg.id, card, "Allow")
    last = relay.webex.bot_messages()[-1]
    assert last.markdown == "⚠️ OpenCode could not be reached; the request is still open."
    assert last.parent_id == (await relay.service.session(session.session_key)).webex_thread_id
    await relay.press(card_msg.id, card, "Allow")
    assert relay.adapter.approvals == [("r1", "allow")]


async def test_cards_of_a_dead_agent_or_ended_session_say_so(relay: Relay) -> None:
    session = await _ask_approval(relay)
    card_msg, _ = relay.webex.last_card()
    await relay.frontend.renderer.handle(
        ApprovalResolved(
            harness=H, session_key=session.session_key, request_id="r1", decision="deny", by="abandoned"
        )
    )
    assert relay.webex.messages[card_msg.id].markdown == "🔌 The agent went away; request closed"

    await _ask_question(relay)
    question_msg, _ = relay.webex.last_card()
    await relay.frontend.renderer.handle(
        QuestionAnswered(harness=H, session_key=session.session_key, request_id="q1", answer=None, by="ended")
    )
    assert relay.webex.messages[question_msg.id].markdown == "🏁 The session ended; request closed"


async def test_wizard_inputs_are_checked_on_the_server(relay: Relay) -> None:
    await relay.say("/new opencode proj m1")
    msg, card = relay.webex.last_card()

    async def tap(**extra: JsonValue) -> str:
        await relay.press(msg.id, card, "Start", **extra)
        return relay.bot_texts()[-1]

    assert "Unknown workspace `elsewhere`" in await tap(value="go", ws="elsewhere")
    assert "has no model `m9`" in await tap(value="go", model="m9")
    assert "Unknown app `nope`" in await tap(value="go", app="nope")
    assert "too long" in await tap(value="x" * (cards.MAX_PROMPT_CHARS + 1))
    assert "unexpected" in await tap(value="go", ws=7)
    assert relay.adapter.started == []
    await relay.press(msg.id, card, "Start", value="go")
    assert [s.prompt for s in relay.adapter.started] == ["go"]


async def test_a_disabled_app_cannot_be_started_from_a_card(relay: Relay) -> None:
    await relay.say("/new")
    msg, card = relay.webex.last_card()
    await relay.press(msg.id, card, "Next", value="cursor")
    assert relay.bot_texts()[-1] == "⚠️ Cursor CLI is not enabled."
    assert relay.adapter.started == []


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
