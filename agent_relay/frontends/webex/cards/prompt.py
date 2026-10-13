"""The last `/new` wizard step: ask what the session should do."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import card, submit, text


def prompt(carry: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return card(
        [
            text("What should it do?", weight="Bolder"),
            {"type": "Input.Text", "id": "value", "isMultiline": True, "placeholder": "Prompt"},
        ],
        [submit("Start", {"relay": "new", "step": "prompt", **carry}, "positive")],
    )
