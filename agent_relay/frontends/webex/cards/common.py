"""What every card builder shares: the envelope, the element helpers, size limits and the text fallback."""

from __future__ import annotations

import copy
import json

from pydantic import JsonValue

from agent_relay.core.service.discovery import is_hidden_model as is_hidden_model  # built in core, shown here
from agent_relay.hook.shims import abbreviate

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
VERSION = "1.3"
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


def input_text(
    field: str,
    hint: str,
    max_length: int,
    value: str | None = None,
    *,
    label: str | None = None,
    required_message: str | None = None,
) -> JsonValue:
    """A multiline text box. `required_message` makes it required, with that message shown when empty."""
    block: dict[str, JsonValue] = {
        "type": "Input.Text",
        "id": field,
        "isMultiline": True,
        "placeholder": hint,
        "maxLength": max_length,
    }
    if label:
        block["label"] = label
    if required_message:
        block["isRequired"] = True
        block["errorMessage"] = required_message
    if value:
        block["value"] = value
    return block


def input_toggle(field: str, title: str, on: bool = False) -> JsonValue:
    return {
        "type": "Input.Toggle",
        "id": field,
        "title": title,
        "value": "true" if on else "false",
        "valueOn": "true",
        "valueOff": "false",
    }


def choice_set(
    field: str,
    choices: list[tuple[str, str]],
    style: str,
    default: str | None = None,
    *,
    limit: int = MAX_CHOICES,
    label: str | None = None,
    required_message: str | None = None,
) -> JsonValue:
    """An Input.ChoiceSet of at most `limit` entries (the default always among them); callers
    say when some were left out."""
    shown = choices[:limit]
    if default is not None and all(value != default for value, _ in shown):
        shown = [*[c for c in choices if c[0] == default][:1], *shown[: limit - 1]]
    block: dict[str, JsonValue] = {
        "type": "Input.ChoiceSet",
        "id": field,
        "style": style,
        "choices": [{"title": title[:CHOICE_TITLE_CHARS], "value": value} for value, title in shown],
    }
    if label:
        block["label"] = label
    if required_message:
        block["isRequired"] = True
        block["errorMessage"] = required_message
    if default is not None:
        block["value"] = default
    return block


def action_set(actions: list[JsonValue]) -> JsonValue:
    return {"type": "ActionSet", "actions": actions}


def submit(title: str, data: dict[str, JsonValue], style: str | None = None) -> JsonValue:
    action: dict[str, JsonValue] = {"type": "Action.Submit", "title": title, "data": data}
    if style:
        action["style"] = style
    return action


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


def duration_text(seconds: float) -> str:
    whole = max(0, round(seconds))
    if whole < 60:
        return f"{whole}s"
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def footer_line(seconds: float | None, tool_calls: int, files: int, added: int, removed: int) -> str:
    """`⏱ 3m · 🔧 14 · 📝 4 files (+120 −8)`: a part is left out when it has nothing to say."""
    parts: list[str] = []
    if seconds is not None:
        parts.append(f"⏱ {duration_text(seconds)}")
    if tool_calls:
        parts.append(f"🔧 {tool_calls}")
    if files:
        parts.append(f"📝 {files} file{'' if files == 1 else 's'} (+{added} −{removed})")
    return " · ".join(parts)


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
