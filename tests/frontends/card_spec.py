"""A checker for the Adaptive Card subset Webex renders: `check_card(card)` returns the problems found.

Webex supports Adaptive Cards 1.3 only, with no `Action.ToggleVisibility`, and answers HTTP 400 for
anything it does not know. The checks mirror what it refuses, so a builder that passes here is very
likely to post; `scripts/card_check.py` is the live proof.
"""

from __future__ import annotations

from pydantic import JsonValue

from agent_relay.frontends.webex.cards.common import CARD_LIMIT_BYTES, card_fallback, minify

MAX_VERSION = (1, 3)
CONTAINERS = {"Container", "ColumnSet", "Column"}
LEAVES = {"TextBlock", "FactSet", "Image", "RichTextBlock"}
INPUTS = {"Input.Text", "Input.Number", "Input.Date", "Input.Time", "Input.Toggle", "Input.ChoiceSet"}
ACTIONS = {"Action.Submit", "Action.OpenUrl", "Action.ShowCard"}
CHOICE_STYLES = {"compact", "expanded"}


def _version(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None


def _dicts(value: JsonValue | None) -> list[dict[str, JsonValue]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


class _Check:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.ids: set[str] = set()

    def fail(self, message: str) -> None:
        self.problems.append(message)

    def card(self, card: dict[str, JsonValue], *, nested: bool) -> None:
        if card.get("type") != "AdaptiveCard":
            self.fail(f"card type is {card.get('type')!r}, not AdaptiveCard")
        version = _version(card.get("version"))
        if version is None or version > MAX_VERSION:
            self.fail(f"card version {card.get('version')!r} is above 1.3")
        if not nested and not isinstance(card.get("$schema"), str):
            self.fail("top-level card has no $schema")
        if not isinstance(card.get("body"), list):
            self.fail("card body is not a list")
        if not isinstance(card.get("actions", []), list):
            self.fail("card actions is not a list")
        self.elements(_dicts(card.get("body")))
        self.actions(_dicts(card.get("actions")))

    def elements(self, elements: list[dict[str, JsonValue]]) -> None:
        for element in elements:
            kind = element.get("type")
            if kind in LEAVES:
                self.leaf(element)
            elif kind in INPUTS:
                self.input(element)
            elif kind == "ActionSet":
                actions = _dicts(element.get("actions"))
                if not actions:
                    self.fail("ActionSet without actions")
                self.actions(actions)
            elif kind in CONTAINERS:
                self.elements(_dicts(element.get("items")))
                self.elements(_dicts(element.get("columns")))
            else:
                self.fail(f"element type {kind!r} is not allowed")

    def leaf(self, element: dict[str, JsonValue]) -> None:
        if element.get("type") == "TextBlock" and not isinstance(element.get("text"), str):
            self.fail("TextBlock without text")

    def input(self, element: dict[str, JsonValue]) -> None:
        kind = element.get("type")
        field = element.get("id")
        if not isinstance(field, str) or not field:
            self.fail(f"{kind} without an id")
        elif field in self.ids:
            self.fail(f"duplicate input id {field!r}")
        else:
            self.ids.add(field)
        if "isRequired" in element and not isinstance(element["isRequired"], bool):
            self.fail(f"input {field!r}: isRequired is not a boolean")
        if "label" in element and not isinstance(element["label"], str):
            self.fail(f"input {field!r}: label is not text")
        if kind == "Input.ChoiceSet":
            self.choice_set(element, str(field))
        if "isMultiline" in element and kind != "Input.Text":
            self.fail(f"input {field!r}: isMultiline is only for Input.Text")

    def choice_set(self, element: dict[str, JsonValue], field: str) -> None:
        choices = _dicts(element.get("choices"))
        if not choices:
            self.fail(f"choice set {field!r} has no choices")
        if element.get("style", "compact") not in CHOICE_STYLES:
            self.fail(f"choice set {field!r}: style {element.get('style')!r} is not allowed")
        values = [c.get("value") for c in choices]
        for choice in choices:
            if not isinstance(choice.get("title"), str) or not isinstance(choice.get("value"), str):
                self.fail(f"choice set {field!r}: a choice needs a text title and value")
        if len(set(map(str, values))) != len(values):
            self.fail(f"choice set {field!r}: duplicate choice values")
        preset = element.get("value")
        if preset is not None and preset not in values:
            self.fail(f"choice set {field!r}: the preset {preset!r} is not one of its choices")

    def actions(self, actions: list[dict[str, JsonValue]]) -> None:
        for action in actions:
            kind = action.get("type")
            if kind not in ACTIONS:
                self.fail(f"action type {kind!r} is not allowed")
                continue
            if not isinstance(action.get("title"), str):
                self.fail(f"{kind} without a title")
            if kind == "Action.Submit" and not isinstance(action.get("data", {}), dict):
                self.fail("Action.Submit data is not an object")
            if kind == "Action.OpenUrl" and not isinstance(action.get("url"), str):
                self.fail("Action.OpenUrl without a url")
            if kind == "Action.ShowCard":
                inner = action.get("card")
                if isinstance(inner, dict):
                    self.card(inner, nested=True)
                else:
                    self.fail("Action.ShowCard without a card")


def check_card(card: dict[str, JsonValue]) -> list[str]:
    """Every way `card` leaves the Webex subset; an empty list means it is valid."""
    check = _Check()
    check.card(card, nested=False)
    size = len(minify(card).encode("utf-8")) + len(card_fallback(card).encode("utf-8"))
    if size > CARD_LIMIT_BYTES:
        check.fail(f"card and fallback text are {size} bytes; Webex allows {CARD_LIMIT_BYTES}")
    return check.problems
