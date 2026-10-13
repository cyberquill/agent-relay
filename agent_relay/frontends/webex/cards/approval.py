"""The approval card: Allow, Allow for session, Deny."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import card, submit, text


def approval(request_id: str, session_label: str, detail: str, deadline_min: int) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {"relay": "approval", "request_id": request_id}
    return card(
        [
            text("Approval needed", weight="Bolder"),
            text(session_label, subtle=True),
            text(detail),
            text(f"Denied automatically in {deadline_min} min.", subtle=True),
        ],
        [
            submit("Allow", {**base, "decision": "allow"}, "positive"),
            submit("Allow for session", {**base, "decision": "allow_session"}),
            submit("Deny", {**base, "decision": "deny"}, "destructive"),
        ],
    )
