"""AC-12: Adaptive Cards v1.3 with Input.ChoiceSet and Action.Submit, one card per message,
each card edited exactly once with its outcome."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from agent_relay.frontends.webex import cards
from agent_relay.models.events import ApprovalRequested, Harness, QuestionAsked
from agent_relay.models.sessions import PendingRequest, SessionRecord
from tests.frontends.conftest import Relay

H = Harness.OPENCODE
ALLOWED_ELEMENTS = {"TextBlock", "Input.ChoiceSet", "Input.Text"}


def _walk(card: dict[str, JsonValue]) -> None:
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.3"
    body = card["body"]
    actions = card["actions"]
    assert isinstance(body, list) and isinstance(actions, list) and actions
    for block in body:
        assert isinstance(block, dict) and block["type"] in ALLOWED_ELEMENTS
        if block["type"] == "Input.ChoiceSet":
            choices = block["choices"]
            assert isinstance(choices, list) and all(
                isinstance(c, dict) and {"title", "value"} <= c.keys() for c in choices
            )
    for action in actions:
        assert isinstance(action, dict) and action["type"] == "Action.Submit"
        data = action["data"]
        assert isinstance(data, dict) and isinstance(data["relay"], str)


def test_builders_produce_valid_v13_cards() -> None:
    _walk(cards.approval("r1", "label", "rm -rf /tmp/x", 10))
    _walk(cards.question("q1", "label", "Which?", ("a", "b")))
    _walk(cards.question("q2", "label", "Free text?", ()))
    _walk(cards.choose("app", "Which app?", [("opencode", "OpenCode")], {"ws": "proj"}))
    _walk(cards.prompt({"app": "opencode"}))


def test_choose_carries_previous_answers() -> None:
    card = cards.choose("ws", "Which?", [("proj", "proj")], {"app": "opencode"})
    actions = card["actions"]
    assert isinstance(actions, list) and isinstance(actions[0], dict)
    assert actions[0]["data"] == {"relay": "new", "step": "ws", "app": "opencode"}


async def _pending(relay: Relay, kind: str, request_id: str) -> SessionRecord:
    session = await relay.registry.upsert_session(
        SessionRecord(session_key="opencode:c1", harness=H, harness_id="c1", workspace="/w/p")
    )
    await relay.registry.add_pending(
        PendingRequest(
            request_id=request_id,
            kind="approval" if kind == "approval" else "question",
            session_key=session.session_key,
            payload={"source": "adapter"},
            deadline=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    return session


async def test_approval_card_is_edited_once_with_outcome(relay: Relay) -> None:
    session = await _pending(relay, "approval", "r1")
    await relay.frontend.renderer.handle(
        ApprovalRequested(
            harness=H, session_key=session.session_key, request_id="r1", tool="bash", detail="git push"
        )
    )
    card_msg, card = relay.webex.last_card()
    _walk(card)
    assert sum(1 for m in relay.webex.bot_messages() if m.attachments) == 1
    await relay.click(card_msg.id, {"relay": "approval", "request_id": "r1", "decision": "allow"})
    await relay.click(card_msg.id, {"relay": "approval", "request_id": "r1", "decision": "deny"})
    assert relay.webex.edits[card_msg.id] == 1
    assert relay.webex.messages[card_msg.id].markdown == "✅ Allowed"
    assert relay.adapter.approvals == [("r1", "allow")]
    assert relay.bot_texts()[-1] == "That request was already answered."


async def test_question_card_answer_and_skip(relay: Relay) -> None:
    session = await _pending(relay, "question", "q1")
    await relay.frontend.renderer.handle(
        QuestionAsked(
            harness=H,
            session_key=session.session_key,
            request_id="q1",
            question="Which db?",
            options=("pg", "sqlite"),
        )
    )
    card_msg, _ = relay.webex.last_card()
    await relay.click(
        card_msg.id, {"relay": "question", "request_id": "q1", "op": "answer", "choice": "pg", "custom": ""}
    )
    assert relay.adapter.answers == [("q1", "pg")]
    assert relay.webex.messages[card_msg.id].markdown == "💬 Answered: pg"
    assert relay.webex.edits[card_msg.id] == 1


async def test_question_custom_text_wins_and_skip_sends_none(relay: Relay) -> None:
    session = await _pending(relay, "question", "q2")
    await relay.registry.add_pending(
        PendingRequest(
            request_id="q3",
            kind="question",
            session_key=session.session_key,
            payload={"source": "adapter"},
            deadline=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await relay.click(
        "m", {"relay": "question", "request_id": "q2", "op": "answer", "choice": "pg", "custom": "mysql"}
    )
    await relay.click("m", {"relay": "question", "request_id": "q3", "op": "skip"})
    assert relay.adapter.answers == [("q2", "mysql"), ("q3", None)]
