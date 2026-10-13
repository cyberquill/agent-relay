"""Helpers for tests that read cards: find a button anywhere on a card, and fill in inputs like a client."""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import JsonValue


def _dicts(value: JsonValue | None) -> list[dict[str, JsonValue]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def actions_of(card: dict[str, JsonValue]) -> Iterator[dict[str, JsonValue]]:
    """Every action on a card: card-level, in body ActionSets, and inside Action.ShowCard cards."""
    found = _dicts(card.get("actions"))
    for block in _dicts(card.get("body")):
        if block.get("type") == "ActionSet":
            found += _dicts(block.get("actions"))
    for action in found:
        yield action
        nested = action.get("card")
        if action.get("type") == "Action.ShowCard" and isinstance(nested, dict):
            yield from actions_of(nested)


def find_action(card: dict[str, JsonValue], title: str) -> dict[str, JsonValue]:
    for action in actions_of(card):
        if action.get("title") == title:
            return action
    raise AssertionError(f"no button {title!r}")


def show_cards(card: dict[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    """Title -> nested card, for every Action.ShowCard on a card."""
    found: dict[str, dict[str, JsonValue]] = {}
    for action in actions_of(card):
        nested = action.get("card")
        if action.get("type") == "Action.ShowCard" and isinstance(nested, dict):
            found[str(action["title"])] = nested
    return found


def inputs_of(card: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    """Every Input.* element on a card, nested ShowCard cards included."""
    found = [b for b in _dicts(card.get("body")) if str(b.get("type", "")).startswith("Input.")]
    for action in actions_of(card):
        nested = action.get("card")
        if action.get("type") == "Action.ShowCard" and isinstance(nested, dict):
            found += [b for b in _dicts(nested.get("body")) if str(b.get("type", "")).startswith("Input.")]
    return found


def defaults(card: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """What a client submits when the user changes nothing: each input's initial value."""
    return {str(i["id"]): i.get("value", "") for i in inputs_of(card)}


def choice_values(block: dict[str, JsonValue]) -> list[str]:
    return [str(c["value"]) for c in _dicts(block.get("choices"))]


def choice_titles(block: dict[str, JsonValue]) -> list[str]:
    return [str(c["title"]) for c in _dicts(block.get("choices"))]


def input_by_id(card: dict[str, JsonValue], field: str) -> dict[str, JsonValue]:
    for block in inputs_of(card):
        if block.get("id") == field:
            return block
    raise AssertionError(f"no input {field!r}")
