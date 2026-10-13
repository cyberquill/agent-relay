"""What every card builder shares: the envelope, the element helpers, size limits and the text fallback."""

from __future__ import annotations

import copy
import json

from pydantic import JsonValue

from agent_relay.hook.shims import abbreviate

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
VERSION = "1.3"
WIZARD_FIELDS = ("app", "ws", "model", "prompt")
CARD_LIMIT_BYTES = 22_740  # Webex: message text + card JSON
MAX_CHOICES = 25
CHOICE_TITLE_CHARS = 100
MAX_ANSWER_CHARS = 4_000
MAX_PROMPT_CHARS = 20_000
SHRUNK_TEXT_CHARS = 2_000


def card(body: list[JsonValue], actions: list[JsonValue]) -> dict[str, JsonValue]:
    return {"type": "AdaptiveCard", "$schema": SCHEMA, "version": VERSION, "body": body, "actions": actions}


def text(
    value: str, *, weight: str | None = None, wrap: bool = True, subtle: bool = False, mono: bool = False
) -> JsonValue:
    block: dict[str, JsonValue] = {"type": "TextBlock", "text": value, "wrap": wrap}
    if weight:
        block["weight"] = weight
    if subtle:
        block["isSubtle"] = True
    if mono:
        block["fontType"] = "Monospace"
    return block


def input_text(field: str, hint: str, max_length: int) -> JsonValue:
    return {
        "type": "Input.Text",
        "id": field,
        "isMultiline": True,
        "placeholder": hint,
        "maxLength": max_length,
    }


def choice_set(
    field: str, choices: list[tuple[str, str]], style: str, default: str | None = None
) -> JsonValue:
    """An Input.ChoiceSet of at most MAX_CHOICES entries; callers say when some were left out."""
    block: dict[str, JsonValue] = {
        "type": "Input.ChoiceSet",
        "id": field,
        "style": style,
        "choices": [
            {"title": label[:CHOICE_TITLE_CHARS], "value": value} for value, label in choices[:MAX_CHOICES]
        ],
    }
    if default is not None:
        block["value"] = default
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


def minify(value: JsonValue) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def message_bytes(markdown: str, card_json: dict[str, JsonValue]) -> int:
    return len(markdown.encode("utf-8")) + len(minify(card_json).encode("utf-8"))


def _shrink(source: dict[str, JsonValue]) -> dict[str, JsonValue]:
    shrunk = copy.deepcopy(source)
    body = shrunk.get("body")
    for block in body if isinstance(body, list) else []:
        if not isinstance(block, dict):
            continue
        choices = block.get("choices")
        if isinstance(choices, list):
            block["choices"] = choices[:MAX_CHOICES]
        value = block.get("text")
        if block.get("type") == "TextBlock" and isinstance(value, str):
            block["text"] = abbreviate(value, SHRUNK_TEXT_CHARS)
    return shrunk


def fit_card(markdown: str, source: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    """The card if text + card JSON fit Webex's limit (shrunk if need be); None means send text only."""
    if message_bytes(markdown, source) <= CARD_LIMIT_BYTES:
        return source
    shrunk = _shrink(source)
    return shrunk if message_bytes(markdown, shrunk) <= CARD_LIMIT_BYTES else None
