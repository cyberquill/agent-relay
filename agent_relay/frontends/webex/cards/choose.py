"""One `/new` wizard step that picks from a list: app, workspace or model."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import card, submit, text


def choose(
    step: str, title: str, choices: list[tuple[str, str]], carry: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    return card(
        [
            text(title, weight="Bolder"),
            {
                "type": "Input.ChoiceSet",
                "id": "value",
                "style": "compact",
                "value": choices[0][0] if choices else "",
                "choices": [{"title": label, "value": value} for value, label in choices],
            },
        ],
        [submit("Next", {"relay": "new", "step": step, **carry}, "positive")],
    )
