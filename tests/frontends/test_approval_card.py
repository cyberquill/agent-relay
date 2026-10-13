"""The approval card's choices and what each submit does: scope of the allow, deny with a reason, and the
server-side checks on both."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import JsonValue

from agent_relay.core.service.approvals import REASON_MAX_CHARS, workspace_grant_active
from agent_relay.frontends.webex import cards
from agent_relay.frontends.webex.cards.approval import grant_label
from agent_relay.models.events import ApprovalRequested, Harness
from agent_relay.models.sessions import PendingRequest, SessionRecord
from tests.frontends.conftest import Relay

H = Harness.OPENCODE
TOKEN = "ghp_" + "f" * 36
GRANT_TITLE = "Allow this tool in this workspace for 4 h"


def _actions(card: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    actions = card["actions"]
    assert isinstance(actions, list)
    return [a for a in actions if isinstance(a, dict)]


def _data(action: dict[str, JsonValue]) -> dict[str, JsonValue]:
    data = action["data"]
    assert isinstance(data, dict)
    return dict(data)


def _show_card(card: dict[str, JsonValue]) -> dict[str, JsonValue]:
    (show,) = [a for a in _actions(card) if a["type"] == "Action.ShowCard"]
    inner = show["card"]
    assert isinstance(inner, dict)
    return inner


async def _ask(relay: Relay, request_id: str = "r1", tool: str = "bash") -> SessionRecord:
    session = await relay.registry.get_session("opencode:c1") or await relay.registry.create_session(
        SessionRecord(session_key="opencode:c1", harness=H, harness_id="c1", workspace="/w/p")
    )
    await relay.registry.add_pending(
        PendingRequest(
            request_id=request_id,
            kind="approval",
            session_key=session.session_key,
            payload={"source": "adapter", "tool": tool},
            deadline=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await relay.frontend.renderer.handle(
        ApprovalRequested(
            harness=H, session_key=session.session_key, request_id=request_id, tool=tool, detail="git push"
        )
    )
    return session


async def _press_deny_with_reason(relay: Relay, **extra: JsonValue) -> None:
    message, card = relay.webex.last_card()
    inner = _show_card(card)
    (submit,) = _actions(inner)
    await relay.click(message.id, {**_data(submit), **extra})


# the card


def test_the_card_offers_the_scoped_choices_in_order_and_nothing_else() -> None:
    card = cards.approval("r1", "label", "rm -rf /tmp/x", "10m", nonce="n")
    actions = _actions(card)
    assert [a["title"] for a in actions] == [
        "Allow once",
        "Allow for this session",
        GRANT_TITLE,
        "Deny",
        "Deny with reason…",
    ]
    assert [_data(a)["decision"] for a in actions[:4]] == [
        "allow",
        "allow_session",
        "allow_workspace",
        "deny",
    ]
    assert "always" not in str(card).lower()


def test_the_detail_and_the_auto_deny_line_stay() -> None:
    card = cards.approval("r1", "label", "echo `hi` *x*", "7m", nonce="n")
    body = card["body"]
    assert isinstance(body, list)
    detail, countdown = body[2], body[3]
    assert (
        isinstance(detail, dict) and detail["text"] == "echo `hi` *x*" and detail["fontType"] == "Monospace"
    )
    assert isinstance(countdown, dict) and countdown["text"] == "Auto-deny in 7m."


def test_deny_with_reason_is_a_show_card_with_a_capped_text_box_and_a_deny_submit() -> None:
    card = cards.approval("r1", "label", "x", "10m", nonce="n9")
    inner = _show_card(card)
    assert inner["type"] == "AdaptiveCard" and inner["version"] == "1.3"
    body = inner["body"]
    assert isinstance(body, list) and len(body) == 1
    box = body[0]
    assert isinstance(box, dict)
    assert (box["type"], box["id"], box["maxLength"]) == ("Input.Text", "reason", REASON_MAX_CHARS)
    (submit,) = _actions(inner)
    assert _data(submit) == {"relay": "approval", "request_id": "r1", "nonce": "n9", "decision": "deny"}


@pytest.mark.parametrize(
    ("seconds", "label"),
    [(14_400, "4 h"), (3_600, "1 h"), (5_400, "1 h 30 min"), (2_700, "45 min"), (60, "1 min"), (61, "2 min")],
)
def test_the_grant_length_on_the_button_follows_the_config(seconds: int, label: str) -> None:
    assert grant_label(seconds) == label
    card = cards.approval("r1", "label", "x", "10m", nonce="n", grant_s=seconds)
    assert _actions(card)[2]["title"] == f"Allow this tool in this workspace for {label}"


# submits


async def test_allow_in_workspace_answers_allow_workspace_and_records_the_grant(relay: Relay) -> None:
    await _ask(relay)
    message, card = relay.webex.last_card()
    await relay.press(message.id, card, GRANT_TITLE)
    assert relay.adapter.approvals == [("r1", "allow_workspace")]
    assert relay.webex.messages[message.id].markdown == "✅ Allowed"
    assert await workspace_grant_active(relay.registry, "/w/p", "bash")
    assert await workspace_grant_active(relay.registry, "/w/p/sub", "bash")
    assert not await workspace_grant_active(relay.registry, "/w/q", "bash")
    assert not await workspace_grant_active(relay.registry, "/w/p", "edit")


async def test_deny_with_reason_sends_the_reason_with_the_deny(relay: Relay) -> None:
    await _ask(relay)
    await _press_deny_with_reason(relay, reason="  use the\nstaging db ")
    await relay.router.join()
    assert relay.adapter.approvals == [("r1", "deny")]
    assert relay.adapter.reasons == [("r1", "use the staging db")]
    message, _ = relay.webex.last_card()
    assert relay.webex.messages[message.id].markdown == "❌ Denied"


async def test_a_plain_deny_has_no_reason(relay: Relay) -> None:
    await _ask(relay)
    message, card = relay.webex.last_card()
    await relay.press(message.id, card, "Deny")
    assert relay.adapter.approvals == [("r1", "deny")] and relay.adapter.reasons == []


async def test_a_deny_with_an_empty_reason_box_is_a_plain_deny(relay: Relay) -> None:
    await _ask(relay)
    await _press_deny_with_reason(relay, reason="")
    assert relay.adapter.approvals == [("r1", "deny")] and relay.adapter.reasons == []


async def test_the_reason_is_scrubbed_before_it_goes_anywhere(relay: Relay) -> None:
    await _ask(relay)
    await _press_deny_with_reason(relay, reason=f"token {TOKEN} leaked")
    ((_, sent),) = relay.adapter.reasons
    assert TOKEN not in sent and sent.startswith("token ") and sent.endswith(" leaked")


@pytest.mark.parametrize("decision", ["Allow once", "Allow for this session", GRANT_TITLE])
async def test_a_reason_typed_in_the_box_is_ignored_by_the_allow_buttons(relay: Relay, decision: str) -> None:
    await _ask(relay)
    message, card = relay.webex.last_card()
    await relay.press(message.id, card, decision, reason="typed, then changed my mind")
    ((_, chosen),) = relay.adapter.approvals
    assert chosen != "deny" and relay.adapter.reasons == []


async def test_a_reason_that_is_too_long_is_refused_and_the_card_stays_usable(relay: Relay) -> None:
    await _ask(relay)
    await _press_deny_with_reason(relay, reason="x" * (REASON_MAX_CHARS + 1))
    assert "too long" in relay.bot_texts()[-1]
    assert relay.adapter.approvals == []
    await _press_deny_with_reason(relay, reason="x" * REASON_MAX_CHARS)
    assert relay.adapter.reasons == [("r1", "x" * REASON_MAX_CHARS)]


@pytest.mark.parametrize("junk", [123, ["a"], {"a": "b"}, True])
async def test_a_reason_that_is_not_text_is_refused(relay: Relay, junk: JsonValue) -> None:
    await _ask(relay)
    await _press_deny_with_reason(relay, reason=junk)
    assert relay.bot_texts()[-1] == "⚠️ That reason was not text."
    assert relay.adapter.approvals == []


@pytest.mark.parametrize("decision", ["always", "allow_forever", "", "ALLOW", "desk"])
async def test_a_decision_the_card_never_offered_is_refused(relay: Relay, decision: str) -> None:
    await _ask(relay)
    message, card = relay.webex.last_card()
    await relay.press(message.id, card, "Allow once", decision=decision)
    assert relay.bot_texts()[-1] == "⚠️ That is not a decision this card offers."
    assert relay.adapter.approvals == []
