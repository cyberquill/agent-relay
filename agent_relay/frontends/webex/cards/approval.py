"""The approval card: Allow, Allow for session, Deny."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import card, submit, text


def approval(
    request_id: str, session_label: str, detail: str, deadline: str, *, nonce: str
) -> dict[str, JsonValue]:
    """`deadline` is the time left (`10m`); `detail` is monospace so markdown in it stays literal."""
    base: dict[str, JsonValue] = {"relay": "approval", "request_id": request_id, "nonce": nonce}
    return card(
        [
            text("Approval needed", weight="Bolder"),
            text(session_label, subtle=True),
            text(detail, mono=True),
            text(f"Auto-deny in {deadline}.", subtle=True),
        ],
        [
            submit("Allow", {**base, "decision": "allow"}, "positive"),
            submit("Allow for session", {**base, "decision": "allow_session"}),
            submit("Deny", {**base, "decision": "deny"}, "destructive"),
        ],
    )
