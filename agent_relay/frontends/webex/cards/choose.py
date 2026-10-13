"""One `/new` wizard step that picks from a list: app, workspace or model."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import MAX_CHOICES, card, choice_set, submit, text


def choose(
    step: str, title: str, choices: list[tuple[str, str]], carry: dict[str, JsonValue], *, nonce: str
) -> dict[str, JsonValue]:
    body: list[JsonValue] = [
        text(title, weight="Bolder"),
        choice_set("value", choices, "compact", choices[0][0] if choices else ""),
    ]
    if len(choices) > MAX_CHOICES:
        body.append(
            text(
                f"Showing {MAX_CHOICES} of {len(choices)}. Type /new with the {step} to pick another.",
                subtle=True,
            )
        )
    return card(body, [submit("Next", {"relay": "new", "step": step, "nonce": nonce, **carry}, "positive")])
