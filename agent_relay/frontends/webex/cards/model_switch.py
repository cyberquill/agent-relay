"""Bare `/model` in a thread: a picker for the session's model."""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import MAX_CHOICES, card, choice_set, submit, text


def model_switch(
    session_key: str,
    current: str,
    choices: list[tuple[str, str]],
    selected: str | None,
    *,
    nonce: str,
) -> dict[str, JsonValue]:
    body: list[JsonValue] = [
        text(f"Model: {current}", weight="Bolder"),
        choice_set("model", choices, "compact", selected),
    ]
    if len(choices) > MAX_CHOICES:
        body.append(text(f"Showing {MAX_CHOICES} of {len(choices)}. `/model <id>` picks any.", subtle=True))
    data: dict[str, JsonValue] = {"relay": "model", "session": session_key, "nonce": nonce}
    return card(body, [submit("Apply", data, "positive")])
