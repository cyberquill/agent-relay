"""`/panic` asks first; the confirmation is good for a minute."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import card, submit, text

CONFIRM_WINDOW_S = 60


def panic_confirm(*, nonce: str) -> dict[str, JsonValue]:
    body: list[JsonValue] = [
        text("Cancel everything?", weight="Bolder"),
        text(
            "Denies open approvals, stops running turns and pauses the relay. "
            f"This confirmation expires in {CONFIRM_WINDOW_S} seconds.",
            subtle=True,
        ),
    ]
    return card(
        body,
        [
            submit("Cancel everything", {"relay": "panic", "op": "confirm", "nonce": nonce}, "destructive"),
            submit("Cancel", {"relay": "panic", "op": "cancel", "nonce": nonce}),
        ],
    )
