"""Bare `/away` at the top level: pick all workspaces or one, then On or Off."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import MAX_CHOICES, card, choice_set, submit, text

ALL_WORKSPACES = "__all__"


def away_scope(choices: list[tuple[str, str]], *, nonce: str) -> dict[str, JsonValue]:
    """`choices` is (value, title incl. current state), with `ALL_WORKSPACES` first."""
    body: list[JsonValue] = [
        text("Away mode", weight="Bolder"),
        choice_set("scope", choices, "compact", ALL_WORKSPACES),
    ]
    if len(choices) > MAX_CHOICES:
        body.append(text(f"Showing {MAX_CHOICES} of {len(choices)}. `/away on` covers all.", subtle=True))
    return card(
        body,
        [
            submit("On", {"relay": "away", "op": "on", "nonce": nonce}, "positive"),
            submit("Off", {"relay": "away", "op": "off", "nonce": nonce}),
        ],
    )
