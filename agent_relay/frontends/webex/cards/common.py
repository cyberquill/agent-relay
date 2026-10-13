"""What every card builder shares: the envelope, the element helpers and the text fallback."""

from __future__ import annotations

from pydantic import JsonValue

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
VERSION = "1.3"
WIZARD_FIELDS = ("app", "ws", "model", "prompt")


def card(body: list[JsonValue], actions: list[JsonValue]) -> dict[str, JsonValue]:
    return {"type": "AdaptiveCard", "$schema": SCHEMA, "version": VERSION, "body": body, "actions": actions}


def text(value: str, *, weight: str | None = None, wrap: bool = True, subtle: bool = False) -> JsonValue:
    block: dict[str, JsonValue] = {"type": "TextBlock", "text": value, "wrap": wrap}
    if weight:
        block["weight"] = weight
    if subtle:
        block["isSubtle"] = True
    return block


def submit(title: str, data: dict[str, JsonValue], style: str | None = None) -> JsonValue:
    action: dict[str, JsonValue] = {"type": "Action.Submit", "title": title, "data": data}
    if style:
        action["style"] = style
    return action


def carry_fields(values: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Keep only the `/new` wizard fields that carry from one step to the next."""
    return {k: v for k, v in values.items() if k in WIZARD_FIELDS and v not in (None, "")}


def card_fallback(card: dict[str, JsonValue]) -> str:
    """Text shown by clients that cannot render the card (and in notifications)."""
    body = card.get("body")
    if isinstance(body, list):
        for block in body:
            if (
                isinstance(block, dict)
                and block.get("type") == "TextBlock"
                and isinstance(block.get("text"), str)
            ):
                return str(block["text"])
    return "agent-relay"
